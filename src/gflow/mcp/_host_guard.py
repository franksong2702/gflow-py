"""Loopback classification, bare-HTTPS origin validation, and DNS-rebinding Host guard."""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

_Scope = dict[str, Any]
_Receive = Callable[[], Awaitable[dict[str, Any]]]
_Send = Callable[[dict[str, Any]], Awaitable[None]]


def is_loopback(host: str) -> bool:
    """Return True if ``host`` resolves exclusively to a local loopback interface."""
    cleaned = host.strip().strip("[]").lower()
    if not cleaned:
        return False
    if cleaned == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def _split_host_header(host_header: str) -> str:
    """Extract the hostname/IP portion from an HTTP ``Host`` header."""
    raw = host_header.strip()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return ""
        return raw[1:end]
    if raw.count(":") == 1:
        host_part, port_part = raw.rsplit(":", 1)
        if port_part.isdigit():
            return host_part
        return ""
    return raw


def host_header_is_loopback(host_header: str) -> bool:
    """Return True if the HTTP ``Host`` header names a loopback literal or ``localhost``."""
    hostname = _split_host_header(host_header)
    return is_loopback(hostname)


def check_bind_allowed(host: str, *, allow_external: bool, allow_env: str) -> None:
    """Fail closed if ``host`` is empty or non-loopback without explicit opt-in."""
    cleaned = host.strip()
    if not cleaned:
        raise SystemExit("Invalid --host '': host must not be empty.")
    if not is_loopback(cleaned) and not allow_external:
        raise SystemExit(
            f"Refusing to bind the MCP HTTP transport to non-loopback host {cleaned!r}. "
            f"Set {allow_env}=1 (alongside authentication) to opt into an external bind."
        )


def validate_bare_https_origin(raw_url: str, env_name: str, *, allow_loopback_http: bool = False) -> str:
    """Validate that ``raw_url`` is a bare ``https://host[:port]`` origin with no path/query.

    Returns the normalized URL without trailing slash.
    """
    cleaned = raw_url.strip()
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""

    scheme_ok = scheme == "https" or (allow_loopback_http and scheme == "http" and is_loopback(host))
    if not scheme_ok or not parts.netloc:
        raise SystemExit(
            f"{env_name} must be an https:// origin (got {raw_url!r}). "
            "OAuth 2.1 and signed media links require HTTPS."
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise SystemExit(
            f"{env_name} must be a bare origin without a path, query, fragment, or credentials "
            f"(got {raw_url!r}; e.g. use 'https://host.ts.net' rather than 'https://host.ts.net/mcp')."
        )
    return f"{scheme}://{parts.netloc}"


class LoopbackHostGuardMiddleware:
    """ASGI middleware preventing DNS-rebinding attacks against loopback HTTP servers."""

    def __init__(
        self,
        app: Any,
        *,
        allow_external: bool = False,
        allowed_hosts: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.allow_external = allow_external
        normalized: set[str] = set()
        for h in allowed_hosts:
            item = h.strip().lower()
            if item:
                normalized.add(item)
                only_host = _split_host_header(item)
                if only_host:
                    normalized.add(only_host.lower())
        self.allowed_hosts = frozenset(normalized)

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if self.allow_external or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        host = ""
        for name, value in scope.get("headers", ()):
            if name == b"host":
                host = value.decode("latin-1", "replace").strip().lower()
                break

        if host_header_is_loopback(host) or (host and host in self.allowed_hosts):
            await self.app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Host not allowed"})
