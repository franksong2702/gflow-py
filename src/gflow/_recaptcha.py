"""Playwright-backed Enterprise reCAPTCHA & same-context Boq mutation executor for Google Flow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import random
import urllib.parse
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._rpc import BATCHEXECUTE_PATH, FLOW_BASE_URL, FlowHttpRpc
from .exceptions import (
    ERR_INVALID_INPUT,
    ERR_UPSTREAM_ERROR,
    AuthError,
    GFlowError,
    NotFoundError,
    RateLimitedError,
    RpcError,
)

logger = logging.getLogger(__name__)


def _load_playwright_storage_state(storage_state_path: Path) -> dict[str, Any]:
    """Load `storage_state.json` and sanitize cookie attributes for Chromium CDP `Storage.setCookies`."""
    try:
        raw = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"Unable to read Google session file {storage_state_path}: {exc}") from exc

    cleaned_cookies: list[dict[str, Any]] = []
    for c in raw.get("cookies", []) if isinstance(raw, dict) else []:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        name = str(c["name"])
        same_site = c.get("sameSite")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        secure = bool(c.get("secure", False))
        if name.startswith(("__Secure-", "__Host-")) or same_site == "None":
            secure = True
        exp = c.get("expires", -1)
        exp_val = float(exp) if isinstance(exp, (int, float)) else -1.0
        domain = str(c.get("domain") or "")
        if not domain and c.get("url"):
            domain = urlsplit(str(c["url"])).hostname or ".google.com"
        domain = domain or ".google.com"
        path = str(c.get("path") or "/")

        if name.startswith("__Host-"):
            cleaned_cookies.append(
                {
                    "name": name,
                    "value": str(c["value"]),
                    "url": f"https://{domain.lstrip('.')}/",
                    "expires": exp_val,
                    "httpOnly": bool(c.get("httpOnly", True)),
                    "secure": True,
                    "sameSite": same_site,
                }
            )
        else:
            cleaned_cookies.append(
                {
                    "name": name,
                    "value": str(c["value"]),
                    "domain": domain,
                    "path": path,
                    "expires": exp_val,
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": secure,
                    "sameSite": same_site,
                }
            )

    origins = raw.get("origins", []) if isinstance(raw, dict) else []
    return {"cookies": cleaned_cookies, "origins": origins if isinstance(origins, list) else []}


class RecaptchaTokenProvider:
    """Maintains a warm Chrome page on `flow.google.com/project/<id>` to mint
    `grecaptcha.enterprise` tokens (`UPLOAD_IMAGE`, `IMAGE_GENERATION`, `VIDEO_GENERATION`)
    and execute reCAPTCHA-guarded mutations in the exact same browser TLS/cookie context.
    """

    def __init__(
        self,
        storage_state_path: Path,
        *,
        default_sitekey: str | None = None,
        headless: bool = True,
    ) -> None:
        self._storage_state_path = storage_state_path
        self._default_sitekey = default_sitekey
        self._headless = headless
        self._lock = asyncio.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._sitekey: str | None = default_sitekey
        self._wiz_meta: dict[str, str] = {}
        self._req_counter = random.randint(20000, 80000)

    async def _close_unlocked(self) -> None:
        for obj, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._pw, "stop"),
        ):
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._wiz_meta = {}

    async def reset(self) -> None:
        """Drop the cached browser context so the next call reloads fresh cookies."""
        async with self._lock:
            await self._close_unlocked()

    async def _ensure_page(self, project_id: str | None = None) -> Any:
        if self._page is not None and not self._page.is_closed():
            if "accounts.google.com" not in self._page.url and "ServiceLogin" not in self._page.url:
                # If we opened `/?pli=1` earlier without a project_id and now have one, navigate once to `/project/<id>`
                if project_id and "/project/" not in self._page.url:
                    await self._navigate_and_wait_gre(f"{FLOW_BASE_URL}/project/{project_id}")
                return self._page
            await self._close_unlocked()

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise GFlowError(
                "Playwright is required to mint Google Flow generation tokens. "
                "Install with: pip install 'gflow-py[browser]' && playwright install chromium",
                retriable=False,
            ) from exc

        state_dict = _load_playwright_storage_state(self._storage_state_path)
        self._pw = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            self._browser = await self._pw.chromium.launch(
                channel="chrome",
                headless=self._headless,
                args=launch_args,
            )
        except Exception:
            self._browser = await self._pw.chromium.launch(
                headless=self._headless,
                args=launch_args,
            )

        self._context = await self._browser.new_context(
            storage_state=state_dict,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = await self._context.new_page()
        target_url = (
            f"{FLOW_BASE_URL}/project/{project_id}"
            if project_id
            else f"{FLOW_BASE_URL}/?pli=1"
        )
        await self._navigate_and_wait_gre(target_url)
        return self._page

    async def _navigate_and_wait_gre(self, target_url: str) -> None:
        assert self._page is not None
        await self._page.goto(target_url, wait_until="domcontentloaded", timeout=45000)

        if "accounts.google.com" in self._page.url or "ServiceLogin" in self._page.url:
            await self._close_unlocked()
            raise AuthError(
                "Google session expired while initializing Enterprise reCAPTCHA context. "
                "Run 'gflow login' on the host machine."
            )

        for _ in range(30):
            ready = await self._page.evaluate(
                """() => {
                    const wiz = window.WIZ_global_data || {};
                    const ok = !!(window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute);
                    return {
                        ok,
                        sk: wiz.xZbWve || null,
                        at: wiz.SNlM0e || "",
                        fsid: wiz.FdrFJe || "",
                        bl: wiz.cfb2h || "",
                    };
                }"""
            )
            if ready.get("sk"):
                self._sitekey = ready["sk"]
            if ready.get("at") and ready.get("bl"):
                self._wiz_meta = {
                    "at": str(ready["at"]),
                    "fsid": str(ready.get("fsid") or ""),
                    "bl": str(ready["bl"]),
                }
            if ready.get("ok") and self._sitekey:
                break
            await asyncio.sleep(0.35)

        sitekey = self._sitekey or self._default_sitekey
        has_gre = await self._page.evaluate(
            "() => !!(window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute)"
        )
        if sitekey and not has_gre:
            await self._page.evaluate(
                """async ({ sk }) => {
                    await new Promise((resolve, reject) => {
                        const s = document.createElement('script');
                        const url = `https://www.google.com/recaptcha/enterprise.js?trustedtypes=true&render=${encodeURIComponent(sk)}`;
                        if (window.trustedTypes && window.trustedTypes.createPolicy) {
                            try {
                                const p = window.trustedTypes.createPolicy('gflowRecaptcha', { createScriptURL: u => u });
                                s.src = p.createScriptURL(url);
                            } catch (e) {
                                s.setAttribute('src', url);
                            }
                        } else {
                            s.src = url;
                        }
                        s.onload = resolve;
                        s.onerror = reject;
                        document.head.appendChild(s);
                    });
                }""",
                {"sk": sitekey},
            )

    async def _mint_unlocked(self, page: Any, action: str) -> str | None:
        sitekey = self._sitekey or self._default_sitekey
        if not sitekey:
            logger.warning("No reCAPTCHA sitekey discovered on flow.google.com; proceeding without token")
            return None
        try:
            await page.mouse.move(random.randint(240, 960), random.randint(180, 680))
        except Exception:
            pass
        token = await page.evaluate(
            """async ({ sitekey, action }) => {
                return await new Promise((resolve, reject) => {
                    window.grecaptcha.enterprise.ready(() => {
                        window.grecaptcha.enterprise
                            .execute(sitekey, { action })
                            .then(resolve)
                            .catch(reject);
                    });
                });
            }""",
            {"sitekey": sitekey, "action": action},
        )
        return str(token) if token else None

    async def get_token(self, action: str, *, project_id: str | None = None) -> str | None:
        """Mint a fresh Enterprise reCAPTCHA token for `action`, rebuilding once if page staled."""
        async with self._lock:
            for attempt in range(2):
                try:
                    page = await self._ensure_page(project_id=project_id)
                    return await self._mint_unlocked(page, action)
                except AuthError:
                    raise
                except Exception:
                    await self._close_unlocked()
                    if attempt == 1:
                        raise
        return None

    async def call_guarded_rpc(
        self,
        action: str,
        project_id: str,
        build_fn: Callable[[str | None], tuple[str, Any]],
    ) -> Any:
        """Mint an Enterprise reCAPTCHA token AND execute the Boq mutation via same-page `fetch()`.

        Executing the mutation from the exact same Chrome TLS/cookie/WIZ context that minted
        the `grecaptcha.enterprise` token avoids cross-context `PUBLIC_ERROR_UNUSUAL_ACTIVITY`
        false positives on `VIDEO_GENERATION` and `IMAGE_GENERATION`.
        """
        async with self._lock:
            for attempt in range(2):
                try:
                    page = await self._ensure_page(project_id=project_id)
                    token = await self._mint_unlocked(page, action)
                    rpc_id, payload = build_fn(token)
                    self._req_counter += 100000

                    inner_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    f_req = json.dumps(
                        [[[rpc_id, inner_json, None, "generic"]]],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    qs = urllib.parse.urlencode(
                        {
                            "rpcids": rpc_id,
                            "source-path": f"/project/{project_id}",
                            "bl": self._wiz_meta.get("bl", ""),
                            "f.sid": self._wiz_meta.get("fsid", ""),
                            "hl": "en",
                            "_reqid": str(self._req_counter),
                            "rt": "c",
                        }
                    )
                    form_body = (
                        urllib.parse.urlencode(
                            {
                                "f.req": f_req,
                                "at": self._wiz_meta.get("at", ""),
                            }
                        )
                        + "&"
                    )
                    resp = await page.evaluate(
                        """async ({ url, body }) => {
                            const r = await fetch(url, {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                                    'X-Same-Domain': '1',
                                },
                                body,
                                credentials: 'include',
                            });
                            return { status: r.status, text: await r.text() };
                        }""",
                        {"url": f"{FLOW_BASE_URL}{BATCHEXECUTE_PATH}?{qs}", "body": form_body},
                    )
                    status = int(resp["status"])
                    body_text = str(resp["text"])
                    if status in (401, 403):
                        raise AuthError(
                            f"AUTH_EXPIRED: Google Flow rejected session credentials on {rpc_id} (HTTP {status})."
                        )
                    if status == 429:
                        raise RateLimitedError(f"Google Flow rate-limited RPC {rpc_id} (HTTP 429).")
                    if status == 404:
                        raise NotFoundError(f"Google Flow resource not found for {rpc_id} (HTTP 404).")
                    if status >= 400:
                        raise RpcError(
                            f"Google Flow RPC {rpc_id} failed with HTTP {status}: {body_text[:300]}",
                            rpc_id=rpc_id,
                            code=status,
                            error_code=ERR_INVALID_INPUT if status == 400 else ERR_UPSTREAM_ERROR,
                            retriable=status >= 500,
                        )
                    return FlowHttpRpc._parse_batchexecute(rpc_id, body_text)
                except (AuthError, RateLimitedError, NotFoundError, GFlowError) as exc:
                    # Only rebuild page & retry once on browser transport glitches, NOT domain errors
                    if isinstance(exc, GFlowError) and type(exc) is GFlowError and attempt == 0:
                        await self._close_unlocked()
                        continue
                    raise
                except Exception:
                    await self._close_unlocked()
                    if attempt == 1:
                        raise
        raise RpcError("Unreachable call_guarded_rpc state", error_code=ERR_UPSTREAM_ERROR)

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()
