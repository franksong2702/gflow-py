"""SSRF-hardening tests for `gflow.mcp._ssrf.fetch_public_image` (restricted http-mode `image_url`)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from gflow.mcp._ssrf import MAX_REDIRECTS, ImageFetchError, fetch_public_image, is_public_ip

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

INTERNAL_ADDRESSES = [
    "127.0.0.1",
    "10.0.0.5",
    "169.254.169.254",
    "::1",
    "::ffff:10.0.0.1",
    "100.64.0.1",
]


def make_resolver(mapping: dict[str, list[str]]) -> Any:
    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return list(mapping[host])

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def never_called_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected outbound request")

    return httpx.MockTransport(handler)


def png_transport(seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    return httpx.MockTransport(handler)


async def expect_failure(category: str, url: str, **kwargs: Any) -> ImageFetchError:
    kwargs.setdefault("max_bytes", 1024 * 1024)
    with pytest.raises(ImageFetchError) as exc_info:
        await fetch_public_image(url, **kwargs)
    exc = exc_info.value
    assert exc.category == category
    assert str(exc) == f"image_url fetch failed: {category}"
    return exc


# ── Scheme / URL validation ──────────────────────────────────────────────────


async def test_http_scheme_rejected() -> None:
    resolver = make_resolver({"images.example.com": [PUBLIC_V4]})
    await expect_failure(
        "not_https",
        "http://images.example.com/a.png",
        resolver=resolver,
        transport=never_called_transport(),
    )
    assert resolver.calls == []


@pytest.mark.parametrize(
    "url",
    ["ftp://images.example.com/a.png", "file:///etc/passwd", "gopher://images.example.com/", "/a.png"],
)
async def test_non_https_schemes_rejected(url: str) -> None:
    await expect_failure(
        "not_https", url, resolver=make_resolver({}), transport=never_called_transport()
    )


@pytest.mark.parametrize(
    "url",
    ["https://user:pass@images.example.com/a.png", "https://user@images.example.com/a.png"],
)
async def test_userinfo_rejected(url: str) -> None:
    resolver = make_resolver({"images.example.com": [PUBLIC_V4]})
    await expect_failure("userinfo", url, resolver=resolver, transport=never_called_transport())
    assert resolver.calls == []


# ── Address policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("address", INTERNAL_ADDRESSES)
async def test_internal_resolution_rejected(address: str) -> None:
    resolver = make_resolver({"evil.example.com": [address]})
    exc = await expect_failure(
        "blocked_address",
        "https://evil.example.com/a.png",
        resolver=resolver,
        transport=never_called_transport(),
    )
    assert address not in str(exc)
    assert "evil.example.com" not in str(exc)
    assert resolver.calls == [("evil.example.com", 443)]


async def test_mixed_public_and_internal_resolution_rejected() -> None:
    resolver = make_resolver({"evil.example.com": [PUBLIC_V4, "10.0.0.5"]})
    exc = await expect_failure(
        "blocked_address",
        "https://evil.example.com/a.png",
        resolver=resolver,
        transport=never_called_transport(),
    )
    assert "10.0.0.5" not in str(exc)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.png",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/a.png",
        "https://[::1]/a.png",
        "https://[::ffff:10.0.0.1]/a.png",
        "https://100.64.0.1/a.png",
    ],
)
async def test_internal_ip_literal_rejected_without_dns(url: str) -> None:
    resolver = make_resolver({})
    await expect_failure("blocked_address", url, resolver=resolver, transport=never_called_transport())
    assert resolver.calls == []


async def test_resolver_failure_is_generic() -> None:
    # Resolution failure is indistinguishable from a blocked address (no internal DNS probing).
    exc = await expect_failure(
        "blocked_address",
        "https://unknown.example.com/a.png",
        resolver=make_resolver({}),
        transport=never_called_transport(),
    )
    assert "unknown.example.com" not in str(exc)


@pytest.mark.parametrize(
    "address",
    INTERNAL_ADDRESSES
    + [
        "0.0.0.0",
        "::",
        "192.168.1.1",
        "172.16.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        "fe80::1%eth0",
        "fec0::1",
        "ff02::1",
        "64:ff9b::a00:1",
        "2002:a00:1::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "not-an-ip",
    ],
)
def test_is_public_ip_rejects_non_public(address: str) -> None:
    assert is_public_ip(address) is False


@pytest.mark.parametrize("address", [PUBLIC_V4, "8.8.8.8", "1.1.1.1", PUBLIC_V6])
def test_is_public_ip_accepts_public(address: str) -> None:
    assert is_public_ip(address) is True


# ── Successful fetch + IP pinning ────────────────────────────────────────────


async def test_public_address_ok_and_connection_pinned_to_validated_ip() -> None:
    seen: list[httpx.Request] = []
    resolver = make_resolver({"images.example.com": [PUBLIC_V4]})
    data = await fetch_public_image(
        "https://images.example.com/path/a.png?size=large",
        max_bytes=1024,
        resolver=resolver,
        transport=png_transport(seen),
    )
    assert data == PNG_BYTES
    assert len(seen) == 1
    req = seen[0]
    assert req.url.scheme == "https"
    assert req.url.host == PUBLIC_V4
    assert req.url.path == "/path/a.png"
    assert req.url.query == b"size=large"
    assert req.headers["host"] == "images.example.com"
    assert req.extensions.get("sni_hostname") == "images.example.com"


async def test_public_ipv6_address_ok() -> None:
    seen: list[httpx.Request] = []
    data = await fetch_public_image(
        "https://images.example.com/a.png",
        max_bytes=1024,
        resolver=make_resolver({"images.example.com": [PUBLIC_V6]}),
        transport=png_transport(seen),
    )
    assert data == PNG_BYTES
    assert seen[0].url.host == PUBLIC_V6
    assert seen[0].headers["host"] == "images.example.com"


async def test_non_default_port_kept_in_host_header() -> None:
    seen: list[httpx.Request] = []
    await fetch_public_image(
        "https://images.example.com:8443/a.png",
        max_bytes=1024,
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=png_transport(seen),
    )
    assert seen[0].url.host == PUBLIC_V4
    assert seen[0].url.port == 8443
    assert seen[0].headers["host"] == "images.example.com:8443"


async def test_public_ip_literal_ok_without_sni_extension() -> None:
    seen: list[httpx.Request] = []
    resolver = make_resolver({})
    data = await fetch_public_image(
        f"https://{PUBLIC_V4}/a.png", max_bytes=1024, resolver=resolver, transport=png_transport(seen)
    )
    assert data == PNG_BYTES
    assert resolver.calls == []
    assert "sni_hostname" not in seen[0].extensions


# ── Redirects ────────────────────────────────────────────────────────────────


async def test_redirect_to_internal_host_rejected() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["host"] == "images.example.com":
            return httpx.Response(302, headers={"location": "https://internal.example.com/secret"})
        raise AssertionError("must not connect to the redirect target")

    resolver = make_resolver(
        {"images.example.com": [PUBLIC_V4], "internal.example.com": ["10.0.0.5"]}
    )
    exc = await expect_failure(
        "blocked_address",
        "https://images.example.com/a.png",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    assert len(seen) == 1
    assert "10.0.0.5" not in str(exc)
    assert "internal.example.com" not in str(exc)


async def test_redirect_to_metadata_ip_literal_rejected() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            301, headers={"location": "https://169.254.169.254/latest/meta-data/"}
        )

    exc = await expect_failure(
        "blocked_address",
        "https://images.example.com/a.png",
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )
    assert len(seen) == 1
    assert "169.254.169.254" not in str(exc)


async def test_redirect_to_http_rejected() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "http://images.example.com/a.png"})

    await expect_failure(
        "not_https",
        "https://images.example.com/a.png",
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )
    assert len(seen) == 1


async def test_relative_redirect_followed_and_revalidated() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/a.png":
            return httpx.Response(302, headers={"location": "/b.png"})
        return httpx.Response(200, content=PNG_BYTES)

    resolver = make_resolver({"images.example.com": [PUBLIC_V4]})
    data = await fetch_public_image(
        "https://images.example.com/a.png",
        max_bytes=1024,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    assert data == PNG_BYTES
    assert [r.url.path for r in seen] == ["/a.png", "/b.png"]
    assert all(r.url.host == PUBLIC_V4 for r in seen)
    assert all(r.headers["host"] == "images.example.com" for r in seen)
    # Every hop is resolved and validated again.
    assert len(resolver.calls) == 2


def _redirect_chain_transport(total_redirects: int, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        step = int(request.url.path.rsplit("/", 1)[-1])
        if step < total_redirects:
            return httpx.Response(302, headers={"location": f"/r/{step + 1}"})
        return httpx.Response(200, content=PNG_BYTES)

    return httpx.MockTransport(handler)


async def test_redirects_up_to_limit_allowed() -> None:
    seen: list[httpx.Request] = []
    data = await fetch_public_image(
        "https://images.example.com/r/0",
        max_bytes=1024,
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=_redirect_chain_transport(MAX_REDIRECTS, seen),
    )
    assert data == PNG_BYTES
    assert len(seen) == MAX_REDIRECTS + 1


async def test_redirects_beyond_limit_rejected() -> None:
    assert MAX_REDIRECTS == 3
    seen: list[httpx.Request] = []
    await expect_failure(
        "redirect_limit",
        "https://images.example.com/r/0",
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=_redirect_chain_transport(MAX_REDIRECTS + 1, seen),
    )
    assert len(seen) == MAX_REDIRECTS + 1


# ── Size cap / status / timeout ──────────────────────────────────────────────


async def test_response_over_max_bytes_rejected_via_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    await expect_failure(
        "too_large",
        "https://images.example.com/a.png",
        max_bytes=1024,
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )


async def test_streamed_response_over_max_bytes_aborted_early() -> None:
    produced = 0

    async def body():
        nonlocal produced
        for _ in range(1000):
            produced += 1
            yield b"x" * 512

    def handler(request: httpx.Request) -> httpx.Response:
        # Async iterator body: chunked transfer, no Content-Length header.
        return httpx.Response(200, content=body())

    await expect_failure(
        "too_large",
        "https://images.example.com/a.png",
        max_bytes=1024,
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )
    assert produced < 10


async def test_response_exactly_max_bytes_ok() -> None:
    payload = b"y" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    data = await fetch_public_image(
        "https://images.example.com/a.png",
        max_bytes=1024,
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )
    assert data == payload


async def test_http_error_status_is_generic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"backend 10.0.0.5 internal-secret-page")

    exc = await expect_failure(
        "http_status",
        "https://images.example.com/a.png",
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )
    message = str(exc)
    assert "404" not in message
    assert "10.0.0.5" not in message
    assert "internal-secret-page" not in message
    assert PUBLIC_V4 not in message


async def test_slow_upstream_times_out() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, content=PNG_BYTES)

    await expect_failure(
        "timeout",
        "https://images.example.com/a.png",
        timeout=0.05,
        resolver=make_resolver({"images.example.com": [PUBLIC_V4]}),
        transport=httpx.MockTransport(handler),
    )


async def test_error_messages_never_contain_internal_addresses() -> None:
    for address in INTERNAL_ADDRESSES:
        with pytest.raises(ImageFetchError) as exc_info:
            await fetch_public_image(
                "https://evil.example.com/a.png",
                max_bytes=1024,
                resolver=make_resolver({"evil.example.com": [address]}),
                transport=never_called_transport(),
            )
        message = str(exc_info.value)
        assert message.startswith("image_url fetch failed: ")
        assert address not in message
        assert repr(address) not in repr(exc_info.value)


async def test_content_encoding_rejected_before_any_decoding() -> None:
    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):  # real (not pre-read) streamed body
            yield b"\x1f\x8b" + b"x" * 10

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Stream(), headers={"content-encoding": "gzip, gzip"})

    await expect_failure(
        "http_status",
        "https://img.example.com/a.png",
        resolver=make_resolver({"img.example.com": ["93.184.216.34"]}),
        transport=httpx.MockTransport(handler),
    )


async def test_port_zero_rejected() -> None:
    await expect_failure(
        "invalid_url",
        "https://img.example.com:0/a.png",
        resolver=make_resolver({"img.example.com": ["93.184.216.34"]}),
        transport=never_called_transport(),
    )


async def test_falls_back_to_next_validated_address_on_connect_error() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, content=PNG_BYTES)

    data = await fetch_public_image(
        "https://img.example.com/a.png",
        max_bytes=1024 * 1024,
        resolver=make_resolver({"img.example.com": ["93.184.216.34", "93.184.216.35"]}),
        transport=httpx.MockTransport(handler),
    )
    assert data == PNG_BYTES
    assert seen == ["93.184.216.34", "93.184.216.35"]
