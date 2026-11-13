"""SSRF-hardened HTTPS image fetcher for ``flow_upload_image(image_url=...)`` in restricted mode.

Used when the MCP server is exposed over public HTTP (``create_server(restricted=True)``), where
``image_url`` is attacker-controlled input. Guarantees:

* Only ``https://`` URLs without userinfo are accepted.
* The host is resolved with ``asyncio`` ``getaddrinfo`` (or an injected resolver) and **every**
  resolved address must be globally routable: no loopback / RFC1918 / link-local (incl. cloud
  metadata ``169.254.169.254``) / CGNAT ``100.64.0.0/10`` / multicast / reserved / unspecified /
  ULA ``fc00::/7`` / IPv4-mapped, 6to4, Teredo or NAT64 IPv6 forms that embed an IPv4 address.
  ``ipaddress`` flags are checked explicitly, ``is_global`` is required, and an explicit deny list
  is applied on top so behaviour does not depend on the Python version's special-purpose tables.
* Redirects are never followed automatically. At most ``MAX_REDIRECTS`` hops are followed by hand
  and each hop is fully re-validated (scheme, userinfo, DNS, address policy).
* DNS-rebinding defence: the TCP connection goes to the *already validated* IP literal (the request
  URL's host is replaced by that IP). The original hostname is sent in the ``Host`` header and passed
  as the httpcore ``sni_hostname`` request extension, so TLS SNI and certificate verification still
  use the original domain. ``sni_hostname`` is supported from httpcore 0.17 (httpx 0.24). Older
  versions ignore the extension and verify the certificate against the IP literal instead. That
  fails closed (the fetch errors out); it never becomes exploitable.
* Environment proxies are ignored (``trust_env=False``), so ``HTTPS_PROXY`` / ``NO_PROXY`` cannot
  reroute or bypass the pinned connection.
* The body is streamed (``client.stream`` + ``aiter_bytes``) and aborted as soon as it goes over
  ``max_bytes``. Total wall time is capped at ``timeout``.
* Errors are generic (``image_url fetch failed: <category>``). They never echo upstream status
  codes, response bodies, resolved addresses, or internal hostnames.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Optional, Union
from urllib.parse import urlsplit

import httpx

__all__ = [
    "MAX_REDIRECTS",
    "ImageFetchError",
    "Resolver",
    "fetch_public_image",
    "is_public_ip",
]

MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_HTTPS_PORT = 443

#: ``async def resolver(host: str, port: int) -> Iterable[str | IPv4Address | IPv6Address]``
Resolver = Callable[[str, int], Awaitable[Iterable[Any]]]

_IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Explicit deny list. It overlaps the ``ipaddress`` flags on purpose (defence in depth across
# Python versions, whose special-purpose registries differ).
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        # IPv4
        "0.0.0.0/8",  # "this" network / unspecified
        "10.0.0.0/8",  # RFC1918
        "100.64.0.0/10",  # CGNAT shared address space
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local incl. cloud metadata 169.254.169.254
        "172.16.0.0/12",  # RFC1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",  # RFC1918
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "255.255.255.255/32",  # broadcast
        # IPv6
        "::/128",  # unspecified
        "::1/128",  # loopback
        "::/96",  # deprecated IPv4-compatible
        "::ffff:0:0/96",  # IPv4-mapped
        "64:ff9b::/96",  # NAT64 well-known prefix (embeds IPv4)
        "64:ff9b:1::/48",  # local-use NAT64
        "100::/64",  # discard-only
        "2001::/23",  # IETF protocol assignments (incl. Teredo 2001::/32)
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (embeds IPv4)
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "fec0::/10",  # deprecated site-local
        "ff00::/8",  # multicast
    )
)


class ImageFetchError(Exception):
    """Generic, non-leaking failure of an SSRF-hardened image fetch.

    ``category`` is one of: ``invalid_url``, ``not_https``, ``userinfo``, ``dns_error``,
    ``blocked_address``, ``redirect_limit``, ``http_status``, ``too_large``, ``timeout``,
    ``network_error``.
    """

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"image_url fetch failed: {category}")


def _to_ip(value: Any) -> _IPAddress:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    text = str(value).strip()
    # Drop an IPv6 zone id ("fe80::1%eth0"): it can only make an address *less* public.
    text = text.split("%", 1)[0]
    return ipaddress.ip_address(text)


def is_public_ip(value: Any) -> bool:
    """Return True only if ``value`` is a globally routable unicast address that is safe to fetch from."""
    try:
        ip = _to_ip(value)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        # IPv6 forms that embed or tunnel to an IPv4 address are refused outright.
        if ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
            return False
        if ip.is_site_local:
            return False

    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    if any(ip in net for net in _BLOCKED_NETWORKS):
        return False

    return bool(ip.is_global)


async def _default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return [info[4][0] for info in infos]


@dataclass(frozen=True)
class _Target:
    original: httpx.URL
    request_url: httpx.URL
    headers: dict[str, str]
    extensions: dict[str, Any] = field(default_factory=dict)


def _parse_https_url(url: Union[str, httpx.URL]) -> httpx.URL:
    text = str(url).strip()
    if not text:
        raise ImageFetchError("invalid_url")
    if "\\" in text or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in text):
        raise ImageFetchError("invalid_url")
    try:
        split = urlsplit(text)
        parsed = httpx.URL(text)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise ImageFetchError("invalid_url") from None

    if (parsed.scheme or "").lower() != "https" or split.scheme.lower() != "https":
        raise ImageFetchError("not_https")
    if parsed.userinfo or "@" in split.netloc:
        raise ImageFetchError("userinfo")
    if not parsed.host:
        raise ImageFetchError("invalid_url")
    return parsed


async def _prepare_target(url: Union[str, httpx.URL], resolver: Resolver) -> _Target:
    parsed = _parse_https_url(url)

    try:
        # IDNA-encoded, lowercase. IPv6 literals come back without brackets.
        host = parsed.raw_host.decode("ascii")
    except UnicodeDecodeError:
        raise ImageFetchError("invalid_url") from None
    port = parsed.port or _DEFAULT_HTTPS_PORT

    literal: Optional[_IPAddress]
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    addresses: list[_IPAddress]
    if literal is not None:
        addresses = [literal]
    else:
        try:
            resolved = await resolver(host, port)
            addresses = [_to_ip(addr) for addr in resolved]
        except ImageFetchError:
            raise
        except Exception:
            raise ImageFetchError("dns_error") from None
        if not addresses:
            raise ImageFetchError("dns_error")

    # Every resolved address must be public. Otherwise an attacker could mix one public and one
    # internal record and rely on connection-time address selection.
    if not all(is_public_ip(addr) for addr in addresses):
        raise ImageFetchError("blocked_address")

    pinned = addresses[0]
    ip_netloc = f"[{pinned.compressed}]" if pinned.version == 6 else pinned.compressed
    try:
        path_and_query = parsed.raw_path.decode("ascii") or "/"
        request_url = httpx.URL(f"https://{ip_netloc}:{port}{path_and_query}")
    except (httpx.InvalidURL, UnicodeDecodeError, ValueError):
        raise ImageFetchError("invalid_url") from None

    host_label = f"[{host}]" if literal is not None and literal.version == 6 else host
    host_header = host_label if port == _DEFAULT_HTTPS_PORT else f"{host_label}:{port}"
    headers = {
        "Host": host_header,
        "Accept": "image/png,image/jpeg,image/webp,image/*;q=0.8",
        "Accept-Encoding": "identity",
        "User-Agent": "gflow-mcp-image-fetch/1",
    }
    # TLS SNI + certificate hostname verification against the original domain while connecting to
    # the pinned IP. Not needed (and not valid as SNI) when the URL host is already an IP literal.
    extensions: dict[str, Any] = {} if literal is not None else {"sni_hostname": host}
    return _Target(
        original=parsed, request_url=request_url, headers=headers, extensions=extensions
    )


def _redirect_target(base: httpx.URL, location: str) -> httpx.URL:
    try:
        return base.join(location.strip())
    except (httpx.InvalidURL, ValueError, TypeError):
        raise ImageFetchError("invalid_url") from None


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError:
            declared_len = None
        if declared_len is not None and declared_len > max_bytes:
            raise ImageFetchError("too_large")

    buf = bytearray()
    async for chunk in response.aiter_bytes():
        if len(buf) + len(chunk) > max_bytes:
            # Raising here exits `client.stream(...)`, which closes the connection right away.
            raise ImageFetchError("too_large")
        buf.extend(chunk)
    return bytes(buf)


async def _fetch(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    resolver: Resolver,
    transport: Optional[httpx.AsyncBaseTransport],
) -> bytes:
    client_kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "timeout": httpx.Timeout(timeout),
        "trust_env": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        # No keep-alive reuse: every hop opens a fresh connection with its own SNI/cert check.
        client_kwargs["limits"] = httpx.Limits(max_keepalive_connections=0)

    current: Union[str, httpx.URL] = url
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            for hop in range(MAX_REDIRECTS + 1):
                target = await _prepare_target(current, resolver)
                async with client.stream(
                    "GET",
                    target.request_url,
                    headers=target.headers,
                    extensions=target.extensions,
                ) as response:
                    status = response.status_code
                    if status in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise ImageFetchError("http_status")
                        if hop >= MAX_REDIRECTS:
                            raise ImageFetchError("redirect_limit")
                        current = _redirect_target(target.original, location)
                        continue
                    if not 200 <= status < 300:
                        raise ImageFetchError("http_status")
                    return await _read_capped(response, max_bytes)
    except ImageFetchError:
        raise
    except httpx.TimeoutException:
        raise ImageFetchError("timeout") from None
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError):
        raise ImageFetchError("network_error") from None
    raise ImageFetchError("redirect_limit")  # pragma: no cover - loop always returns or raises


async def fetch_public_image(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    resolver: Optional[Resolver] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> bytes:
    """Fetch ``url`` with full SSRF hardening and return the raw body (at most ``max_bytes`` bytes).

    ``resolver`` (``async (host, port) -> iterable of IPs``) and ``transport`` are injection points
    for tests. Production callers leave both as ``None``.

    Raises:
        ImageFetchError: with a generic, non-leaking message ``image_url fetch failed: <category>``.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        return await asyncio.wait_for(
            _fetch(
                url,
                max_bytes=max_bytes,
                timeout=timeout,
                resolver=resolver or _default_resolver,
                transport=transport,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise ImageFetchError("timeout") from None
