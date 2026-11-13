"""Self-hosted single-password OAuth 2.1 Authorization Server for claude.ai custom connectors."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

import anyio
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, TypeAdapter
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from ..paths import _atomic_write_json, get_home_dir
from ._host_guard import validate_bare_https_origin

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ALLOWED_REDIRECT_URIS",
    "MIN_PASSWORD_LEN",
    "OAUTH_ALLOWED_REDIRECTS_ENV",
    "OAUTH_BASE_URL_ENV",
    "OAUTH_PASSWORD_ENV",
    "OAUTH_STATE_PATH_ENV",
    "TRUST_PROXY_ENV",
    "OAuthConfig",
    "SelfHostedOAuthProvider",
    "build_oauth_provider",
    "get_oauth_config",
]

OAUTH_PASSWORD_ENV = "GFLOW_MCP_OAUTH_PASSWORD"
OAUTH_BASE_URL_ENV = "GFLOW_MCP_OAUTH_BASE_URL"
OAUTH_STATE_PATH_ENV = "GFLOW_MCP_OAUTH_STATE_PATH"
TRUST_PROXY_ENV = "GFLOW_MCP_TRUST_PROXY"
OAUTH_ALLOWED_REDIRECTS_ENV = "GFLOW_MCP_OAUTH_ALLOWED_REDIRECTS"

DEFAULT_ALLOWED_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)

MIN_PASSWORD_LEN = 16
MAX_CLIENTS = 100
MAX_PENDING = 20
PENDING_TTL_SECONDS = 300
MAX_LOGIN_ATTEMPTS = 3
THROTTLE_WINDOW_SECONDS = 60
THROTTLE_MAX_FAILURES = 5
MAX_THROTTLE_IPS = 2048

_ANY_URL_ADAPTER: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_acceptable_extra_redirect(uri: str) -> bool:
    """Extra (env-supplied) redirect URIs must be https, or http on a loopback host."""
    if any(c.isspace() for c in uri):
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme == "http":
        return _is_loopback_host(parts.hostname)
    return False


def _normalize_redirect_uri(uri: str) -> str:
    try:
        return str(_ANY_URL_ADAPTER.validate_python(uri))
    except (ValueError, TypeError):
        return uri


def _env_allowed_redirect_uris() -> tuple[str, ...]:
    raw = os.environ.get(OAUTH_ALLOWED_REDIRECTS_ENV, "")
    out: list[str] = []
    for item in raw.split(","):
        uri = item.strip()
        if not uri:
            continue
        if not _is_acceptable_extra_redirect(uri):
            logger.warning(
                "Ignoring %s entry %r: only https or loopback http URIs are accepted",
                OAUTH_ALLOWED_REDIRECTS_ENV,
                uri,
            )
            continue
        out.append(uri)
    return tuple(out)


def _resolve_allowed_redirect_uris(explicit: tuple[str, ...] | None) -> frozenset[str]:
    if explicit is None:
        base: tuple[str, ...] = (*DEFAULT_ALLOWED_REDIRECT_URIS, *_env_allowed_redirect_uris())
    else:
        base = tuple(explicit)
    allowed: set[str] = set()
    for uri in base:
        allowed.add(uri)
        allowed.add(_normalize_redirect_uri(uri))
    return frozenset(allowed)


class _Pending(NamedTuple):
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expiry: float
    attempts: int


@dataclass(frozen=True)
class OAuthConfig:
    password: str = field(repr=False)
    base_url: str = field(repr=True)
    state_path: Path | None = field(default=None)
    trust_proxy: bool = field(default=False)


def _slug_for_base_url(base_url: str) -> str:
    norm = base_url.strip().rstrip("/").lower()
    readable = (re.sub(r"[^a-z0-9._-]", "_", urlsplit(base_url).netloc.lower()) or "origin")[:64]
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"{readable}.{digest}"


def _derived_state_path(base_url: str) -> Path:
    return get_home_dir() / "oauth" / f"{_slug_for_base_url(base_url)}.json"


def get_oauth_config(*, allow_loopback_http: bool = False) -> OAuthConfig | None:
    """Resolve self-hosted OAuth config from environment, or return ``None`` if unset."""
    password = os.environ.get(OAUTH_PASSWORD_ENV) or ""
    base_url = (os.environ.get(OAUTH_BASE_URL_ENV) or "").strip()
    if not password and not base_url:
        return None

    if not password or not base_url:
        missing = OAUTH_PASSWORD_ENV if not password else OAUTH_BASE_URL_ENV
        raise SystemExit(
            f"Self-hosted OAuth is partially configured; set BOTH {OAUTH_PASSWORD_ENV} "
            f"and {OAUTH_BASE_URL_ENV} (or unset both). Missing: {missing}."
        )
    if len(password) < MIN_PASSWORD_LEN:
        raise SystemExit(
            f"{OAUTH_PASSWORD_ENV} must be at least {MIN_PASSWORD_LEN} characters "
            "(use a strong random password)."
        )

    normalized_base = validate_bare_https_origin(
        base_url,
        OAUTH_BASE_URL_ENV,
        allow_loopback_http=allow_loopback_http,
    )
    override = (os.environ.get(OAUTH_STATE_PATH_ENV) or "").strip()
    state_path = Path(override).expanduser() if override else _derived_state_path(normalized_base)
    trust_proxy = os.environ.get(TRUST_PROXY_ENV) == "1"
    return OAuthConfig(
        password=password,
        base_url=normalized_base,
        state_path=state_path,
        trust_proxy=trust_proxy,
    )


def _client_ip(request: Request, *, trust_proxy: bool) -> str:
    if trust_proxy:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
    return request.client.host if request.client else "unknown"


def _normalize_resource(resource: str | None) -> str:
    return (resource or "").rstrip("/")


class SelfHostedOAuthProvider(InMemoryOAuthProvider):
    """Password-gated single-tenant OAuth 2.1 Authorization Server with 0600 persistence."""

    def __init__(
        self,
        password: str,
        base_url: str,
        state_path: Path | None = None,
        trust_proxy: bool = False,
        allowed_redirect_uris: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
        self.__salt = secrets.token_bytes(16)
        self.__pw_digest = self._kdf(password)
        self._kdf_limiter = anyio.CapacityLimiter(4)
        self._state_path = state_path
        self._trust_proxy = trust_proxy
        self._save_lock = asyncio.Lock()
        self._pending: dict[str, _Pending] = {}
        self._fail_times: dict[str, list[float]] = {}
        self._refresh_resources: dict[str, str] = {}
        self._allowed_redirect_uris: frozenset[str] = _resolve_allowed_redirect_uris(
            allowed_redirect_uris
        )
        self._load_state()

    def _kdf(self, password: str) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=self.__salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    def _redirect_uri_allowed(self, uri: str) -> bool:
        return (
            uri in self._allowed_redirect_uris
            or _normalize_redirect_uri(uri) in self._allowed_redirect_uris
        )

    def _client_redirects_allowed(self, client_info: OAuthClientInformationFull) -> bool:
        uris = client_info.redirect_uris
        if not uris:
            return False
        return all(self._redirect_uri_allowed(str(u)) for u in uris)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self._client_redirects_allowed(client_info):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri is not allowed.",
            )
        if client_info.client_id not in self.clients and len(self.clients) >= MAX_CLIENTS:
            used = {t.client_id for t in self.access_tokens.values()}
            used |= {t.client_id for t in self.refresh_tokens.values()}
            evictable = next((cid for cid in self.clients if cid not in used), None)
            if evictable is None:
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="Client registration limit reached.",
                )
            self.clients.pop(evictable, None)
        await super().register_client(client_info)
        await self._save_state()

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if params.redirect_uri is None or not self._redirect_uri_allowed(
            str(params.redirect_uri)
        ):
            raise AuthorizeError(
                error="invalid_request",
                error_description="redirect_uri is not allowed.",
            )
        expected_resource = self._expected_resource()
        allowed_resources = {
            expected_resource,
            _normalize_resource(str(self.base_url)),
        }
        if (
            params.resource is not None
            and _normalize_resource(params.resource) not in allowed_resources
        ):
            raise AuthorizeError(
                error="invalid_request",
                error_description="The requested resource does not match this MCP server.",
            )
        params = params.model_copy(update={"resource": expected_resource})
        self._prune_pending()
        while len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda s: self._pending[s].expiry)
            self._pending.pop(oldest, None)
        sid = secrets.token_urlsafe(32)
        self._pending[sid] = _Pending(client, params, time.time() + PENDING_TTL_SECONDS, 0)
        return f"{str(self.base_url).rstrip('/')}/login?sid={sid}"

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path or "/mcp")
        routes.append(Route("/login", self._login, methods=["GET", "POST"]))
        return routes

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            self._prune_pending()
            sid = request.query_params.get("sid", "")
            entry = self._pending.get(sid)
            redirect = str(entry.params.redirect_uri) if entry else None
            return self._render_form(sid, redirect_uri=redirect)

        ip = _client_ip(request, trust_proxy=self._trust_proxy)
        retry_after = self._throttled(ip)
        if retry_after is not None:
            return Response(
                "Too many attempts. Try again later.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        form = await request.form()
        sid = str(form.get("sid", ""))
        password = str(form.get("password", ""))

        self._prune_pending()
        entry = self._pending.get(sid)
        if entry is None:
            return self._render_form(
                "",
                error="Your login link expired. Restart the connection from your MCP client.",
            )
        client, params, expiry, attempts = entry

        presented = await anyio.to_thread.run_sync(self._kdf, password, limiter=self._kdf_limiter)
        if not hmac.compare_digest(presented, self.__pw_digest):
            self._record_failure(ip)
            attempts += 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                self._pending.pop(sid, None)
                return self._render_form(
                    "",
                    error="Too many failed attempts. Restart the connection from your MCP client.",
                )
            self._pending[sid] = _Pending(client, params, expiry, attempts)
            return self._render_form(
                sid,
                redirect_uri=str(params.redirect_uri),
                error="Incorrect password.",
            )

        self._pending.pop(sid, None)
        self._fail_times.pop(ip, None)
        redirect = await InMemoryOAuthProvider.authorize(self, client, params)
        code = next(iter(parse_qs(urlsplit(redirect).query).get("code", [])), None)
        if code is not None and code in self.auth_codes:
            self.auth_codes[code] = self.auth_codes[code].model_copy(
                update={"resource": params.resource}
            )
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    def _expected_resource(self) -> str:
        resource = (
            str(self._resource_url)
            if getattr(self, "_resource_url", None) is not None
            else f"{str(self.base_url).rstrip('/')}/mcp"
        )
        return _normalize_resource(resource)

    def _render_form(
        self,
        sid: str,
        *,
        redirect_uri: str | None = None,
        error: str = "",
    ) -> HTMLResponse:
        safe_sid = html.escape(sid, quote=True)
        err = (
            f'<div style="color:#dc2626;background:#fef2f2;padding:10px 12px;border-radius:8px;margin-bottom:14px">{html.escape(error)}</div>'
            if error
            else ""
        )
        consent = (
            f"<p style='color:#4b5563;font-size:0.92rem'>Authorizing callback to <code>{html.escape(redirect_uri)}</code></p>"
            if redirect_uri
            else ""
        )
        doc = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Google Flow MCP Connector</title></head>"
            "<body style='font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;display:flex;justify-content:center;padding-top:10vh;margin:0'>"
            "<div style='background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:28px 32px;max-width:440px;width:90%;box-shadow:0 8px 24px rgba(15,23,42,0.06)'>"
            "<h2 style='margin:0 0 8px;color:#0f172a'>Google Flow MCP Connector</h2>"
            "<p style='margin:0 0 16px;color:#475569'>Enter your connector password to authorize access to Google Flow.</p>"
            f"{consent}{err}"
            "<form method='post' action='login'>"
            f"<input type='hidden' name='sid' value='{safe_sid}'>"
            "<input type='password' name='password' placeholder='Connector password' autofocus required "
            "style='box-sizing:border-box;width:100%;font-size:1rem;padding:10px 12px;border:1px solid #cbd5e1;border-radius:8px;margin-bottom:12px'>"
            "<button type='submit' style='width:100%;font-size:1rem;font-weight:600;padding:10px 16px;background:#0f172a;color:#fff;border:none;border-radius:8px;cursor:pointer'>Authorize Connector</button>"
            "</form></div></body></html>"
        )
        status = 401 if error else 200
        headers = {
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'",
        }
        return HTMLResponse(doc, status_code=status, headers=headers)

    def _throttled(self, ip: str) -> int | None:
        now = time.time()
        times = [t for t in self._fail_times.get(ip, []) if now - t < THROTTLE_WINDOW_SECONDS]
        if times:
            self._fail_times[ip] = times
        else:
            self._fail_times.pop(ip, None)
        if len(times) >= THROTTLE_MAX_FAILURES:
            return int(THROTTLE_WINDOW_SECONDS - (now - min(times))) + 1
        return None

    def _record_failure(self, ip: str) -> None:
        if ip not in self._fail_times and len(self._fail_times) >= MAX_THROTTLE_IPS:
            self._fail_times.pop(next(iter(self._fail_times)), None)
        self._fail_times.setdefault(ip, []).append(time.time())

    def _prune_pending(self) -> None:
        now = time.time()
        for sid in [s for s, (_, _, exp, _) in self._pending.items() if exp < now]:
            self._pending.pop(sid, None)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        token = await super().exchange_authorization_code(client, authorization_code)
        resource = authorization_code.resource or self._expected_resource()
        stored_access = self.access_tokens[token.access_token]
        self.access_tokens[token.access_token] = stored_access.model_copy(
            update={"resource": resource}
        )
        if token.refresh_token is not None:
            self._refresh_resources[token.refresh_token] = resource
        await self._save_state()
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        resource = self._refresh_resources.get(refresh_token.token)
        if resource is None or _normalize_resource(resource) != self._expected_resource():
            raise TokenError(
                "invalid_grant",
                "Refresh token is not bound to this MCP resource; reauthorization is required.",
            )
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        self._refresh_resources.pop(refresh_token.token, None)
        stored_access = self.access_tokens[token.access_token]
        self.access_tokens[token.access_token] = stored_access.model_copy(
            update={"resource": resource}
        )
        if token.refresh_token is not None:
            self._refresh_resources[token.refresh_token] = resource
        await self._save_state()
        return token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._refresh_resources.pop(token.token, None)
        else:
            paired = self._access_to_refresh_map.get(token.token)
            if paired is not None:
                self._refresh_resources.pop(paired, None)
        await super().revoke_token(token)
        await self._save_state()

    async def verify_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        access = await super().verify_token(token)
        if access is None or _normalize_resource(access.resource) != self._expected_resource():
            return None
        return access

    async def _save_state(self) -> None:
        if self._state_path is None:
            return
        async with self._save_lock:
            data: dict[str, Any] = {
                "clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
                "access_tokens": {
                    k: v.model_dump(mode="json") for k, v in self.access_tokens.items()
                },
                "refresh_tokens": {
                    k: v.model_dump(mode="json") for k, v in self.refresh_tokens.items()
                },
                "a2r": dict(self._access_to_refresh_map),
                "r2a": dict(self._refresh_to_access_map),
                "refresh_resources": dict(self._refresh_resources),
            }
            await anyio.to_thread.run_sync(self._write_state_file, data)

    def _write_state_file(self, data: dict[str, Any]) -> None:
        assert self._state_path is not None
        try:
            parent = self._state_path.parent
            if os.name == "posix":
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._state_path, data, mode=0o600)
        except OSError as exc:
            logger.warning("Could not persist OAuth state to %s: %s", self._state_path, exc)

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("OAuth state top level is not an object")
            clients = {
                k: OAuthClientInformationFull.model_validate(v)
                for k, v in data.get("clients", {}).items()
            }
            access_tokens = {
                k: AccessToken.model_validate(v)
                for k, v in data.get("access_tokens", {}).items()
            }
            refresh_tokens = {
                k: RefreshToken.model_validate(v)
                for k, v in data.get("refresh_tokens", {}).items()
            }
            a2r = dict(data.get("a2r", {}))
            r2a = dict(data.get("r2a", {}))
            refresh_resources = {
                str(tok): str(res)
                for tok, res in data.get("refresh_resources", {}).items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Ignoring unreadable OAuth state %s: %s", self._state_path, exc)
            return
        dropped = {
            cid for cid, info in clients.items() if not self._client_redirects_allowed(info)
        }
        if dropped:
            logger.warning(
                "Dropping %d persisted OAuth client(s) with non-allowlisted redirect_uri",
                len(dropped),
            )
            clients = {k: v for k, v in clients.items() if k not in dropped}
            access_tokens = {
                k: v for k, v in access_tokens.items() if v.client_id not in dropped
            }
            refresh_tokens = {
                k: v for k, v in refresh_tokens.items() if v.client_id not in dropped
            }
            a2r = {a: r for a, r in a2r.items() if a in access_tokens}
            r2a = {r: a for r, a in r2a.items() if r in refresh_tokens}
        self.clients = clients
        self.access_tokens = access_tokens
        self.refresh_tokens = refresh_tokens
        self._access_to_refresh_map = a2r
        self._refresh_to_access_map = r2a
        self._refresh_resources = refresh_resources

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r}, clients={len(self.clients)})"


def build_oauth_provider(config: OAuthConfig) -> AuthProvider:
    if config.state_path is not None:
        logger.info("OAuth client registry + tokens persist at %s", config.state_path)
    return SelfHostedOAuthProvider(
        password=config.password,
        base_url=config.base_url,
        state_path=config.state_path,
        trust_proxy=config.trust_proxy,
    )
