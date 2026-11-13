"""``gflow-mcp`` CLI entrypoint — launch the Google Flow MCP server (`stdio` or `http`)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from urllib.parse import urlsplit

from ..paths import get_home_dir
from ._auth import MCP_TOKEN_ENV, build_auth, get_configured_token
from ._filelink import (
    DEFAULT_DOWNLOAD_TTL_SECONDS,
    FileLinkSigner,
    FileTransferConfig,
    load_or_create_signing_secret,
)
from ._host_guard import (
    LoopbackHostGuardMiddleware,
    check_bind_allowed,
    is_loopback,
    validate_bare_https_origin,
)
from ._oauth import (
    OAUTH_BASE_URL_ENV,
    OAUTH_PASSWORD_ENV,
    OAuthConfig,
    build_oauth_provider,
    get_oauth_config,
)
from .server import create_server

__all__ = ["main"]

ALLOW_EXTERNAL_BIND_ENV = "GFLOW_MCP_ALLOW_EXTERNAL_BIND"
PUBLIC_URL_ENV = "GFLOW_MCP_PUBLIC_URL"
DOWNLOAD_TTL_ENV = "GFLOW_MCP_DOWNLOAD_TTL_SECONDS"
_VALID_TRANSPORTS = frozenset({"stdio", "http"})


def _configure_logging(level: str) -> None:
    """Route all server logs to stderr so stdout remains clean JSON-RPC in stdio mode."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _check_http_auth_required(host: str, token: str | None, oauth: OAuthConfig | None) -> None:
    if not is_loopback(host) and token is None and oauth is None:
        raise SystemExit(
            f"Refusing to bind gflow-mcp HTTP transport to non-loopback host {host!r} "
            f"without authentication. Set {MCP_TOKEN_ENV} (static bearer) and/or "
            f"{OAUTH_PASSWORD_ENV} + {OAUTH_BASE_URL_ENV} (self-hosted OAuth 2.1)."
        )


def _build_file_transfer() -> FileTransferConfig | None:
    public_url = (os.environ.get(PUBLIC_URL_ENV) or "").strip()
    env_name = PUBLIC_URL_ENV
    if not public_url:
        public_url = (os.environ.get(OAUTH_BASE_URL_ENV) or "").strip()
        env_name = OAUTH_BASE_URL_ENV
    if not public_url:
        return None
    normalized = validate_bare_https_origin(public_url, env_name)

    ttl_raw = (os.environ.get(DOWNLOAD_TTL_ENV) or "").strip()
    ttl = int(ttl_raw) if ttl_raw.isdigit() and int(ttl_raw) >= 60 else DEFAULT_DOWNLOAD_TTL_SECONDS

    secret = load_or_create_signing_secret(get_home_dir() / "filelink_secret.key")
    return FileTransferConfig(
        signer=FileLinkSigner(secret, default_ttl=ttl),
        base_url=normalized,
        download_ttl_seconds=ttl,
    )


def _resolve_port(raw: str) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"Invalid --port {raw!r}: must be an integer.") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"Invalid --port {port}: must be in 1..65535.")
    return port


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gflow-mcp",
        description="Run the Google Flow MCP server (stdio for local IDEs, http for remote agents / claude.ai).",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("GFLOW_PROFILE"),
        help="Google Flow auth profile to bind (default: active profile).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("GFLOW_MCP_TRANSPORT", "stdio"),
        help="MCP transport: 'stdio' (default) or 'http'.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("GFLOW_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("GFLOW_MCP_PORT", "9421"),
        help="HTTP bind port (default: 9421; coexists cleanly with notebooklm-mcp on 9420).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("GFLOW_LOG_LEVEL", "INFO"),
        help="Stderr logging level (default: INFO).",
    )
    return parser


def _configure_mcp_http_body_limit(max_bytes: int = 24 * 1024 * 1024) -> None:
    """Allow up to 24 MiB Streamable HTTP JSON-RPC bodies so 15 MiB `image_base64` uploads succeed cleanly."""
    try:
        import fastmcp.server.http as fh
        from mcp.server.transport_security import RequestBodyLimitMiddleware

        base_cls = fh.FastMCPStreamableHTTPSessionManager
        if getattr(base_cls, "_gflow_max_body_configured", False):
            return

        class _LargeBodySessionManager(base_cls):  # type: ignore[valid-type,misc]
            _gflow_max_body_configured = True

            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self.max_request_body_size = max_bytes
                self.asgi_app = RequestBodyLimitMiddleware(self._handle_request, max_bytes)

        fh.FastMCPStreamableHTTPSessionManager = _LargeBodySessionManager  # type: ignore[misc]
    except Exception:
        pass


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)

    if args.transport not in _VALID_TRANSPORTS:
        raise SystemExit(f"Invalid transport {args.transport!r}: expected 'stdio' or 'http'.")

    if args.transport == "http":
        host = args.host.strip()
        allow_external = os.environ.get(ALLOW_EXTERNAL_BIND_ENV) == "1"
        check_bind_allowed(host, allow_external=allow_external, allow_env=ALLOW_EXTERNAL_BIND_ENV)

        token = get_configured_token()
        oauth_config = get_oauth_config()
        _check_http_auth_required(host, token, oauth_config)

        oauth_provider = build_oauth_provider(oauth_config) if oauth_config else None
        file_transfer = _build_file_transfer()

        server = create_server(
            profile=args.profile,
            auth=build_auth(token, oauth_provider),
            file_transfer=file_transfer,
        )

        has_auth = token is not None or oauth_config is not None
        allowed_hosts: list[str] = []
        if has_auth:
            for candidate in (
                oauth_config.base_url if oauth_config else None,
                file_transfer.base_url if file_transfer else None,
            ):
                if candidate:
                    netloc = urlsplit(candidate).netloc
                    if netloc:
                        allowed_hosts.append(netloc)

        from starlette.middleware import Middleware

        _configure_mcp_http_body_limit()
        server.run(
            transport="http",
            host=host,
            port=_resolve_port(args.port),
            uvicorn_config={"proxy_headers": False},
            middleware=[
                Middleware(
                    LoopbackHostGuardMiddleware,
                    allow_external=(allow_external and has_auth),
                    allowed_hosts=allowed_hosts,
                )
            ],
        )
    else:
        server = create_server(profile=args.profile)
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
