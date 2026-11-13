"""OAuth redirect_uri allowlist hardening for the self-hosted authorization server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull

from gflow.mcp._oauth import (
    DEFAULT_ALLOWED_REDIRECT_URIS,
    MAX_CLIENTS,
    OAUTH_ALLOWED_REDIRECTS_ENV,
    SelfHostedOAuthProvider,
)

PASSWORD = "CorrectHorseBatteryStaple2026!"
BASE_URL = "https://flow-host.example.ts.net:8443"
CLAUDE_AI = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_COM = "https://claude.com/api/mcp/auth_callback"
EVIL = "https://evil.example/cb"


@pytest.fixture(autouse=True)
def _clear_allowlist_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OAUTH_ALLOWED_REDIRECTS_ENV, raising=False)


def _provider(state_path: Path | None = None, **kwargs: object) -> SelfHostedOAuthProvider:
    return SelfHostedOAuthProvider(
        password=PASSWORD,
        base_url=BASE_URL,
        state_path=state_path,
        **kwargs,  # type: ignore[arg-type]
    )


def _params(redirect_uri: str) -> AuthorizationParams:
    return AuthorizationParams(
        state="xyz-state",
        scopes=[],
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=True,
    )


def test_default_allowlist_contains_claude_callbacks() -> None:
    assert CLAUDE_AI in DEFAULT_ALLOWED_REDIRECT_URIS
    assert CLAUDE_COM in DEFAULT_ALLOWED_REDIRECT_URIS


async def test_claude_callbacks_allowed(tmp_path: Path) -> None:
    state_file = tmp_path / "oauth" / "state.json"
    provider = _provider(state_file)

    ai = OAuthClientInformationFull(client_id="claude-ai", redirect_uris=[CLAUDE_AI])
    com = OAuthClientInformationFull(client_id="claude-com", redirect_uris=[CLAUDE_COM])
    await provider.register_client(ai)
    await provider.register_client(com)

    assert "claude-ai" in provider.clients
    assert "claude-com" in provider.clients
    assert state_file.exists()

    login_url = await provider.authorize(ai, _params(CLAUDE_AI))
    assert login_url.startswith(f"{BASE_URL}/login?sid=")


async def test_evil_redirect_rejected(tmp_path: Path) -> None:
    state_file = tmp_path / "oauth" / "state.json"
    provider = _provider(state_file)
    client = OAuthClientInformationFull(client_id="evil", redirect_uris=[EVIL])

    with pytest.raises(RegistrationError) as exc_info:
        await provider.register_client(client)

    assert exc_info.value.error == "invalid_redirect_uri"
    assert exc_info.value.error_description == "redirect_uri is not allowed."
    assert "evil" not in provider.clients
    assert not state_file.exists()


async def test_mixed_redirect_list_rejected() -> None:
    provider = _provider()
    client = OAuthClientInformationFull(
        client_id="mixed",
        redirect_uris=[CLAUDE_AI, EVIL],
    )

    with pytest.raises(RegistrationError) as exc_info:
        await provider.register_client(client)

    assert exc_info.value.error == "invalid_redirect_uri"
    assert "mixed" not in provider.clients


async def test_rejection_happens_before_eviction() -> None:
    provider = _provider()
    for i in range(MAX_CLIENTS):
        provider.clients[f"c{i}"] = OAuthClientInformationFull(
            client_id=f"c{i}", redirect_uris=[CLAUDE_AI]
        )
    before = set(provider.clients)

    with pytest.raises(RegistrationError) as exc_info:
        await provider.register_client(
            OAuthClientInformationFull(client_id="newcomer", redirect_uris=[EVIL])
        )

    assert exc_info.value.error == "invalid_redirect_uri"
    assert set(provider.clients) == before


async def test_env_extends_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        OAUTH_ALLOWED_REDIRECTS_ENV,
        " https://my-client.example/oauth/cb , http://127.0.0.1:33418/callback,"
        "http://evil.example/cb,,",
    )
    provider = _provider()

    await provider.register_client(
        OAuthClientInformationFull(
            client_id="custom", redirect_uris=["https://my-client.example/oauth/cb"]
        )
    )
    await provider.register_client(
        OAuthClientInformationFull(
            client_id="loopback", redirect_uris=["http://127.0.0.1:33418/callback"]
        )
    )
    await provider.register_client(
        OAuthClientInformationFull(client_id="claude-web", redirect_uris=[CLAUDE_AI])
    )
    assert {"custom", "loopback", "claude-web"} <= set(provider.clients)

    # Non-loopback plain http from the env var must be ignored.
    with pytest.raises(RegistrationError):
        await provider.register_client(
            OAuthClientInformationFull(
                client_id="plain-http", redirect_uris=["http://evil.example/cb"]
            )
        )
    with pytest.raises(RegistrationError):
        await provider.register_client(
            OAuthClientInformationFull(client_id="evil", redirect_uris=[EVIL])
        )
    # Exact match only: a different path on an allowed host is rejected.
    with pytest.raises(RegistrationError):
        await provider.register_client(
            OAuthClientInformationFull(
                client_id="path-trick", redirect_uris=["https://my-client.example/oauth/cb2"]
            )
        )


async def test_explicit_allowlist_overrides_defaults() -> None:
    provider = _provider(allowed_redirect_uris=("https://only.example/cb",))

    await provider.register_client(
        OAuthClientInformationFull(client_id="only", redirect_uris=["https://only.example/cb"])
    )
    assert "only" in provider.clients

    with pytest.raises(RegistrationError):
        await provider.register_client(
            OAuthClientInformationFull(client_id="claude-web", redirect_uris=[CLAUDE_AI])
        )


async def test_authorize_rejects_non_allowlisted_redirect() -> None:
    provider = _provider()
    legacy = OAuthClientInformationFull(client_id="legacy", redirect_uris=[EVIL])
    # Simulate a historical client that bypassed registration checks.
    provider.clients["legacy"] = legacy

    with pytest.raises(AuthorizeError) as exc_info:
        await provider.authorize(legacy, _params(EVIL))

    assert exc_info.value.error == "invalid_request"
    assert provider._pending == {}


async def test_load_state_drops_clients_with_malicious_redirect(tmp_path: Path) -> None:
    state_file = tmp_path / "oauth" / "state.json"
    state_file.parent.mkdir(parents=True)

    good = OAuthClientInformationFull(client_id="good", redirect_uris=[CLAUDE_AI])
    evil = OAuthClientInformationFull(client_id="evil", redirect_uris=[EVIL])
    mixed = OAuthClientInformationFull(client_id="mixed", redirect_uris=[CLAUDE_AI, EVIL])
    good_tok = AccessToken(token="tok-good", client_id="good", scopes=[], expires_at=None)
    evil_tok = AccessToken(token="tok-evil", client_id="evil", scopes=[], expires_at=None)

    data = {
        "clients": {
            "good": good.model_dump(mode="json"),
            "evil": evil.model_dump(mode="json"),
            "mixed": mixed.model_dump(mode="json"),
        },
        "access_tokens": {
            "tok-good": good_tok.model_dump(mode="json"),
            "tok-evil": evil_tok.model_dump(mode="json"),
        },
    }
    state_file.write_text(json.dumps(data), encoding="utf-8")

    provider = _provider(state_file)

    assert set(provider.clients) == {"good"}
    assert "tok-good" in provider.access_tokens
    assert "tok-evil" not in provider.access_tokens

    # Next persist must not resurrect the dropped clients.
    await provider.register_client(
        OAuthClientInformationFull(client_id="claude-com", redirect_uris=[CLAUDE_COM])
    )
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert "evil" not in saved["clients"]
    assert "mixed" not in saved["clients"]
    assert {"good", "claude-com"} <= set(saved["clients"])


async def test_authorize_rejects_allowlisted_uri_not_registered_by_this_client(tmp_path) -> None:
    from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
    from mcp.shared.auth import OAuthClientInformationFull

    from gflow.mcp._oauth import SelfHostedOAuthProvider

    provider = SelfHostedOAuthProvider(
        password="CorrectHorseBatteryStaple2026!",
        base_url="https://flow-host.example.ts.net:8443",
        state_path=tmp_path / "state.json",
    )
    client = OAuthClientInformationFull(
        client_id="a", redirect_uris=["https://claude.ai/api/mcp/auth_callback"]
    )
    await provider.register_client(client)
    params = AuthorizationParams(
        state="s",
        scopes=[],
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        redirect_uri="https://claude.com/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
    )
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, params)
