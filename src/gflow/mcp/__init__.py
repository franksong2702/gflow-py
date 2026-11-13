"""Dual-mode FastMCP server (`stdio` + remote `http` with OAuth 2.1) for Google Flow."""

from __future__ import annotations

import os

# Normalize blank FASTMCP_STATELESS_HTTP before FastMCP imports pydantic-settings.
if os.environ.get("FASTMCP_STATELESS_HTTP") == "":
    os.environ.pop("FASTMCP_STATELESS_HTTP", None)

from .server import SERVER_INSTRUCTIONS, SERVER_NAME, create_server

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "create_server"]
