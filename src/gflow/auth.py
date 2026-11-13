"""Authentication, automatic cookie rotation (`RotateCookies`), session recovery, and browser login."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from ._rpc import FLOW_BASE_URL
from .exceptions import AuthError, GFlowError
from .paths import (
    _atomic_write_json,
    get_active_profile,
    get_browser_profile_dir,
    get_profile_dir,
    get_storage_state_path,
)

logger = logging.getLogger(__name__)

REQUIRED_AUTH_COOKIES = ("SID", "__Secure-1PSID")

ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
_ROTATE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://accounts.google.com",
}
_ROTATE_BODY = '[000,"-0000000000000000000"]'

# Default proactive keepalive interval (9 minutes, inside Google's 600s `identity.hfcr` window)
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 540.0


def load_cookies_from_storage(storage_path: Path) -> httpx.Cookies:
    """Load `.google.com` & `flow.google.com` cookies from a Playwright `storage_state.json`."""
    if not storage_path.exists():
        raise AuthError(
            f"Authentication file not found at {storage_path}. "
            "Run 'gflow login' (or 'gflow login --from-notebooklm') to authenticate."
        )
    try:
        raw = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"Failed to parse storage_state.json at {storage_path}: {exc}") from exc

    cookie_list = raw.get("cookies") if isinstance(raw, dict) else None
    if not isinstance(cookie_list, list):
        raise AuthError(
            f"Invalid storage_state.json format at {storage_path}: missing 'cookies' list."
        )

    jar = httpx.Cookies()
    found_names: set[str] = set()
    for entry in cookie_list:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        domain = entry.get("domain", "")
        path = entry.get("path", "/")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if "google" in domain:
            jar.set(name, value, domain=domain, path=path)
            found_names.add(name)

    missing = [c for c in REQUIRED_AUTH_COOKIES if c not in found_names]
    if missing:
        raise AuthError(
            f"Missing required Google session cookies ({', '.join(missing)}) in {storage_path}. "
            "Run 'gflow login' to re-authenticate."
        )
    return jar


def _merge_jar_into_storage_file(storage_path: Path, jar: httpx.Cookies) -> None:
    """Persist rotated cookies from ``jar`` back into Playwright ``storage_state.json`` atomically."""
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8")) if storage_path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    existing = data.get("cookies")
    if not isinstance(existing, list):
        existing = []

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for row in existing:
        if not isinstance(row, dict):
            continue
        k = (
            str(row.get("name") or ""),
            str(row.get("domain") or ""),
            str(row.get("path") or "/"),
        )
        if k[0]:
            if k not in by_key:
                order.append(k)
            by_key[k] = dict(row)

    for c in jar.jar:
        if not c.name or c.value is None:
            continue
        domain = c.domain or ".google.com"
        path = c.path or "/"
        k = (c.name, domain, path)
        prev = by_key.get(k, {})
        same_site = prev.get("sameSite", "None")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        is_secure = bool(
            c.secure
            or prev.get("secure", False)
            or c.name.startswith(("__Secure-", "__Host-"))
            or same_site == "None"
        )
        if c.name.startswith("__Host-"):
            domain = domain.lstrip(".")
            path = "/"
        updated: dict[str, Any] = {
            **prev,
            "name": c.name,
            "value": c.value,
            "domain": domain,
            "path": path,
            "expires": float(c.expires) if c.expires else prev.get("expires", -1),
            "httpOnly": prev.get("httpOnly", True),
            "secure": is_secure,
            "sameSite": same_site,
        }
        if k not in by_key:
            order.append(k)
        by_key[k] = updated

    data["cookies"] = [by_key[k] for k in order]
    if "origins" not in data:
        data["origins"] = []
    _atomic_write_json(storage_path, data, mode=0o600)


async def rotate_google_cookies(
    storage_path: Path,
    *,
    source_jar: httpx.Cookies | None = None,
) -> httpx.Cookies | None:
    """Call ``accounts.google.com/RotateCookies`` to refresh ``__Secure-1PSIDTS`` & ``SIDCC``.

    On HTTP 200, persists rotated cookies to ``storage_path`` (`0600`) and returns the fresh jar.
    Returns ``None`` if Google rejects rotation (e.g. HTTP 401 when PSIDTS already expired).
    """
    try:
        base_jar = source_jar if source_jar is not None else load_cookies_from_storage(storage_path)
    except AuthError:
        return None

    # Clone jar into an isolated client so failed rotation does not mutate caller state
    fresh_jar = httpx.Cookies()
    for c in base_jar.jar:
        if c.name and c.value is not None:
            fresh_jar.set(c.name, c.value, domain=c.domain or "", path=c.path or "/")

    try:
        async with httpx.AsyncClient(
            cookies=fresh_jar,
            follow_redirects=True,
            timeout=12.0,
        ) as client:
            resp = await client.post(
                ROTATE_COOKIES_URL,
                headers=_ROTATE_HEADERS,
                content=_ROTATE_BODY,
            )
            if resp.status_code != 200:
                return None
            rotated_jar = client.cookies
            _merge_jar_into_storage_file(storage_path, rotated_jar)
            return load_cookies_from_storage(storage_path)
    except Exception as exc:
        logger.debug("RotateCookies attempt failed: %s", exc)
        return None


async def _verify_jar_against_flow(jar: httpx.Cookies) -> bool:
    """Quick check whether ``jar`` loads ``flow.google.com`` with a live ``SNlM0e`` XSRF token."""
    try:
        async with httpx.AsyncClient(
            cookies=jar,
            follow_redirects=True,
            timeout=12.0,
        ) as client:
            resp = await client.get(f"{FLOW_BASE_URL}/?pli=1")
            url = str(resp.url)
            if "accounts.google.com" in url or "ServiceLogin" in url:
                return False
            return '"SNlM0e":"' in resp.text
    except Exception:
        return False


async def _headless_remint_from_browser_profile(
    browser_dir: Path,
    dest_storage: Path,
) -> httpx.Cookies | None:
    """Attempt silent headless cookie re-mint from a persistent Chromium profile directory."""
    if not browser_dir.is_dir():
        return None
    # Check if profile actually has Cookies sqlite DB (i.e. was logged into before)
    has_cookies_db = (browser_dir / "Default" / "Cookies").exists() or (browser_dir / "Cookies").exists()
    if not has_cookies_db:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(browser_dir),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(f"{FLOW_BASE_URL}/?pli=1", wait_until="domcontentloaded", timeout=20000)
                if "accounts.google.com" in page.url or "ServiceLogin" in page.url:
                    return None
                for _ in range(15):
                    snlm0e = await page.evaluate(
                        "() => (window.WIZ_global_data && window.WIZ_global_data.SNlM0e) || null"
                    )
                    if snlm0e:
                        await asyncio.sleep(0.8)
                        dest_storage.parent.mkdir(parents=True, exist_ok=True)
                        await context.storage_state(path=str(dest_storage))
                        try:
                            os.chmod(dest_storage, 0o600)
                        except OSError:
                            pass
                        jar = load_cookies_from_storage(dest_storage)
                        rotated = await rotate_google_cookies(dest_storage, source_jar=jar)
                        return rotated or jar
                    await asyncio.sleep(0.4)
                return None
            finally:
                await context.close()
    except Exception as exc:
        logger.debug("Headless browser_profile re-mint skipped/failed (%s): %s", browser_dir, exc)
        return None


async def recover_google_session(
    *,
    profile: str,
    storage_path: Path,
) -> httpx.Cookies:
    """Multi-rung automatic Google session renewal (L1/L2/L3).

    Raises :class:`AuthError` (`error_code="AUTH_EXPIRED"`) immediately if all rungs fail.
    """
    relogin_cmd = "gflow login" if profile == "default" else f"gflow login --profile {profile}"
    if os.environ.get("GFLOW_DISABLE_AUTO_RENEW") == "1":
        raise AuthError(
            f"AUTH_EXPIRED: Google Flow session for profile '{profile}' has expired "
            f"(auto-renew disabled). Run '{relogin_cmd}' on the server host.",
            details={"profile": profile, "relogin_command": relogin_cmd},
        )

    # Rung 0: Reload storage_path from disk in case another process already rotated it
    if storage_path.exists():
        try:
            disk_jar = load_cookies_from_storage(storage_path)
            if await _verify_jar_against_flow(disk_jar):
                rotated = await rotate_google_cookies(storage_path, source_jar=disk_jar)
                return rotated or disk_jar
        except AuthError:
            pass

        # Rung 1: Direct RotateCookies POST against storage_path
        rotated = await rotate_google_cookies(storage_path)
        if rotated is not None and await _verify_jar_against_flow(rotated):
            logger.info("Recovered Google Flow session via RotateCookies (Rung 1).")
            return rotated

    # Rung 2: Sync from active co-located ~/.notebooklm profile if present & valid
    if os.environ.get("GFLOW_DISABLE_SIBLING_AUTH_SYNC") != "1":
        sibling_candidates = [
            Path.home() / ".notebooklm" / "profiles" / profile / "storage_state.json",
        ]
        if profile == "default":
            sibling_candidates.append(
                Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"
            )
        for candidate in sibling_candidates:
            if candidate.exists() and candidate.resolve() != storage_path.resolve():
                try:
                    sib_jar = load_cookies_from_storage(candidate)
                    if await _verify_jar_against_flow(sib_jar):
                        shutil.copyfile(candidate, storage_path)
                        try:
                            os.chmod(storage_path, 0o600)
                        except OSError:
                            pass
                        rotated = await rotate_google_cookies(storage_path, source_jar=sib_jar)
                        logger.info("Recovered Google Flow session from active sibling profile %s (Rung 2).", candidate)
                        return rotated or sib_jar
                except Exception:
                    continue

    # Rung 3: Headless persistent browser_profile silent SSO re-mint
    if os.environ.get("GFLOW_DISABLE_HEADLESS_REAUTH") != "1":
        candidate_dirs = [
            get_profile_dir(profile, create=False) / "browser_profile",
            Path.home() / ".notebooklm" / "profiles" / profile / "browser_profile",
        ]
        if profile == "default":
            candidate_dirs.append(
                Path.home() / ".notebooklm" / "profiles" / "default" / "browser_profile"
            )
        for bdir in candidate_dirs:
            reminted = await _headless_remint_from_browser_profile(bdir, storage_path)
            if reminted is not None and await _verify_jar_against_flow(reminted):
                logger.info("Recovered Google Flow session via headless browser_profile %s (Rung 3).", bdir)
                return reminted

    raise AuthError(
        f"AUTH_EXPIRED: Google Flow login session for profile '{profile}' has expired and "
        "automatic renewal (RotateCookies + sibling sync + headless browser_profile re-mint) "
        f"could not recover it. Please notify the operator to run '{relogin_cmd}' on the server host.",
        details={"profile": profile, "relogin_command": relogin_cmd},
    )


def import_storage_state(
    source_path: str | Path,
    *,
    profile: str | None = None,
) -> Path:
    """Import an existing Playwright `storage_state.json` (e.g. from `~/.notebooklm`)."""
    src = Path(source_path).expanduser()
    load_cookies_from_storage(src)
    dest = get_storage_state_path(profile)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def find_notebooklm_storage_state(profile: str = "default") -> Path:
    candidates = [
        Path.home() / ".notebooklm" / "profiles" / profile / "storage_state.json",
        Path.home() / ".notebooklm" / "storage_state.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise AuthError(
        "No NotebookLM storage_state.json found under ~/.notebooklm/. "
        "Run 'gflow login' to sign in via browser."
    )


async def login_via_browser(
    *,
    profile: str | None = None,
    browser_channel: str = "chromium",
    timeout_seconds: int = 300,
    fresh: bool = False,
    storage_override: Path | None = None,
) -> Path:
    """Launch an interactive browser window for Google Flow sign-in and save `storage_state.json`."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise GFlowError(
            "Playwright is not installed. Run: pip install 'gflow-py[browser]' && playwright install chromium",
            retriable=False,
        ) from exc

    resolved_profile = get_active_profile(profile)
    browser_dir = get_browser_profile_dir(resolved_profile)
    if fresh and browser_dir.exists():
        shutil.rmtree(browser_dir)
        browser_dir.mkdir(parents=True, exist_ok=True)

    dest = storage_override or get_storage_state_path(resolved_profile)
    dest.parent.mkdir(parents=True, exist_ok=True)

    channel = None if browser_channel == "chromium" else browser_channel

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(browser_dir),
            channel=channel,
            headless=False,
            viewport={"width": 1360, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"{FLOW_BASE_URL}/", wait_until="domcontentloaded")

            deadline = time.monotonic() + timeout_seconds
            authenticated = False
            while time.monotonic() < deadline:
                for pg in context.pages:
                    if pg.is_closed():
                        continue
                    url = pg.url
                    if url.startswith(FLOW_BASE_URL) and "accounts.google.com" not in url:
                        try:
                            snlm0e = await pg.evaluate(
                                "() => (window.WIZ_global_data && window.WIZ_global_data.SNlM0e) || null"
                            )
                            if snlm0e:
                                authenticated = True
                                break
                        except Exception:
                            pass
                if authenticated:
                    await asyncio.sleep(1.5)
                    await context.storage_state(path=str(dest))
                    try:
                        os.chmod(dest, 0o600)
                    except OSError:
                        pass
                    await rotate_google_cookies(dest)
                    return dest
                await asyncio.sleep(1.0)

            raise AuthError(
                f"Timed out waiting {timeout_seconds}s for Google Flow login in browser."
            )
        finally:
            await context.close()


def doctor_check(profile: str | None = None, explicit_storage: str | Path | None = None) -> dict[str, Any]:
    """Inspect local profile configuration, storage_state cookies, and Playwright readiness."""
    prof = get_active_profile(profile)
    prof_dir = get_profile_dir(prof, create=False)
    storage_path = get_storage_state_path(prof, explicit_storage=explicit_storage)

    cookies_ok = False
    cookie_error = None
    cookie_count = 0
    if storage_path.exists():
        try:
            jar = load_cookies_from_storage(storage_path)
            cookie_count = len(jar)
            cookies_ok = True
        except Exception as exc:
            cookie_error = str(exc)

    playwright_installed = False
    try:
        import playwright  # noqa: F401

        playwright_installed = True
    except ImportError:
        pass

    return {
        "active_profile": prof,
        "profile_dir": str(prof_dir),
        "storage_state_path": str(storage_path),
        "storage_exists": storage_path.exists(),
        "cookies_valid": cookies_ok,
        "cookie_count": cookie_count,
        "cookie_error": cookie_error,
        "playwright_installed": playwright_installed,
    }
