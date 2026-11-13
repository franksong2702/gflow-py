"""Constant-time Bearer token verifier + MultiAuth composition for HTTP MCP."""

from __future__ import annotations

import hashlib
import hmac
import os

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier

__all__ = [
    "MCP_TOKEN_ENV",
    "McpBearerAuthProvider",
    "build_auth",
    "build_auth_provider",
    "get_configured_token",
]

MCP_TOKEN_ENV = "GFLOW_MCP_TOKEN"
_CLIENT_ID = "gflow-mcp"


def get_configured_token() -> str | None:
    """Return configured bearer token from ``$GFLOW_MCP_TOKEN``, or ``None`` if unset."""
    token = os.environ.get(MCP_TOKEN_ENV)
    if token is None:
        return None
    token = token.strip()
    return token or None


class McpBearerAuthProvider(TokenVerifier):
    """Constant-time SHA-256 digest bearer verifier that never retains cleartext secrets."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.__digest = hashlib.sha256(token.encode("utf-8")).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            presented = token.encode("latin-1")
        except UnicodeEncodeError:
            return None
        if hmac.compare_digest(hashlib.sha256(presented).digest(), self.__digest):
            return AccessToken(token=_CLIENT_ID, client_id=_CLIENT_ID, scopes=[])
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(token=<redacted>)"


def build_auth_provider(token: str | None) -> McpBearerAuthProvider | None:
    return McpBearerAuthProvider(token) if token else None


def build_auth(token: str | None, oauth: AuthProvider | None) -> AuthProvider | None:
    """Compose optional static bearer + optional self-hosted OAuth 2.1 via ``MultiAuth``."""
    bearer = build_auth_provider(token)
    if oauth and bearer:
        from fastmcp.server.auth import MultiAuth

        return MultiAuth(server=oauth, verifiers=[bearer])
    return oauth or bearer
