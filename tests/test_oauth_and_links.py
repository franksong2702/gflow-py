"""Security & protocol tests for Bearer auth, OAuth 2.1 AS, Host guard, and signed file links."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from gflow.mcp._auth import McpBearerAuthProvider
from gflow.mcp._filelink import FileLinkError, FileLinkSigner, FileTransferConfig
from gflow.mcp._host_guard import (
    LoopbackHostGuardMiddleware,
    host_header_is_loopback,
    is_loopback,
    validate_bare_https_origin,
)
from gflow.mcp._oauth import SelfHostedOAuthProvider


def test_loopback_and_origin_validation() -> None:
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("flow-host.example.ts.net")

    assert host_header_is_loopback("127.0.0.1:9421")
    assert host_header_is_loopback("[::1]:9421")
    assert not host_header_is_loopback("evil.example:9421")

    assert (
        validate_bare_https_origin("https://flow-host.example.ts.net:8443/", "TEST_URL")
        == "https://flow-host.example.ts.net:8443"
    )
    with pytest.raises(SystemExit):
        validate_bare_https_origin("https://flow-host.example.ts.net:8443/mcp", "TEST_URL")
    with pytest.raises(SystemExit):
        validate_bare_https_origin("http://flow-host.example.ts.net:8443", "TEST_URL")


async def test_host_guard_blocks_dns_rebinding_and_allows_configured_oauth_host() -> None:
    async def ok(_req):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/ping", ok)],
        middleware=[
            Middleware(
                LoopbackHostGuardMiddleware,
                allow_external=False,
                allowed_hosts=["flow-host.example.ts.net:8443"],
            )
        ],
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9421") as c:
        r_local = await c.get("/ping")
        assert r_local.status_code == 200

        r_evil = await c.get("/ping", headers={"Host": "rebind.attacker.example"})
        assert r_evil.status_code == 403

        r_ts = await c.get("/ping", headers={"Host": "flow-host.example.ts.net:8443"})
        assert r_ts.status_code == 200


async def test_bearer_auth_constant_time_digest() -> None:
    verifier = McpBearerAuthProvider("super-secret-bearer-token-12345")
    assert "super-secret" not in repr(verifier)
    assert await verifier.verify_token("super-secret-bearer-token-12345") is not None
    assert await verifier.verify_token("wrong-token") is None


def test_file_link_signer_and_single_use_jti() -> None:
    signer = FileLinkSigner(b"0123456789abcdef0123456789abcdef")
    ft = FileTransferConfig(signer=signer, base_url="https://flow-host.example.ts.net:8443")

    dl_url = ft.download_url_for_media("media-uuid-1")
    dl_token = dl_url.rsplit("/", 1)[-1]
    claims = signer.verify(dl_token, op="dl")
    assert claims["media_id"] == "media-uuid-1"

    # Operation confusion ('dl' token presented to 'ul') must fail
    with pytest.raises(FileLinkError):
        signer.verify(dl_token, op="ul")

    # Tampered signature must fail
    with pytest.raises(FileLinkError):
        signer.verify(dl_token[:-2] + "aa", op="dl")

    # Single-use upload JTI lifecycle
    ul_info = ft.create_upload_link("proj-uuid-1", label="Start frame")
    jti = ul_info["upload_id"]
    assert ft.jti_store.status(jti)["status"] == "awaiting_user_upload"
    assert ft.jti_store.try_begin(jti) is True
    assert ft.jti_store.try_begin(jti) is False  # concurrent replay blocked
    ft.jti_store.commit(jti, 9999999999.0, {"media_id": "new-img-1"})
    assert ft.jti_store.try_begin(jti) is False  # post-commit replay blocked
    assert ft.jti_store.status(jti)["media_id"] == "new-img-1"


async def test_self_hosted_oauth_password_gate_and_persistence(tmp_path: Path) -> None:
    state_file = tmp_path / "oauth" / "state.json"
    password = "CorrectHorseBatteryStaple2026!"
    base_url = "https://flow-host.example.ts.net:8443"

    provider = SelfHostedOAuthProvider(
        password=password,
        base_url=base_url,
        state_path=state_file,
    )
    client = OAuthClientInformationFull(
        client_id="claude-web",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )
    await provider.register_client(client)
    assert state_file.exists()

    params = AuthorizationParams(
        state="xyz-state",
        scopes=[],
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
    )
    login_url = await provider.authorize(client, params)
    sid = parse_qs(urlsplit(login_url).query)["sid"][0]

    app = Starlette(routes=provider.get_routes("/mcp"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        follow_redirects=False,
    ) as http:
        # GET renders consent form with strict CSP
        r_get = await http.get(f"/login?sid={sid}")
        assert r_get.status_code == 200
        assert "claude.ai" in r_get.text
        assert "default-src 'none'" in r_get.headers["content-security-policy"]

        # Wrong password -> 401, does not consume sid on first failure
        r_bad = await http.post("/login", data={"sid": sid, "password": "wrong"})
        assert r_bad.status_code == 401

        # Correct password -> 302 redirect to claude.ai callback with authorization code
        r_ok = await http.post("/login", data={"sid": sid, "password": password})
        assert r_ok.status_code == 302
        loc = r_ok.headers["location"]
        assert loc.startswith("https://claude.ai/api/mcp/auth_callback")
        assert "code=" in loc

        # Replay of consumed sid -> 401 expired
        r_replay = await http.post("/login", data={"sid": sid, "password": password})
        assert r_replay.status_code == 401
