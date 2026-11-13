"""Structured machine-readable MCP error formatting (`P1-5`)."""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from fastmcp.exceptions import ToolError

from ..exceptions import (
    ERR_INVALID_INPUT,
    ERR_UPSTREAM_ERROR,
    GFlowError,
)

P = ParamSpec("P")
R = TypeVar("R")


def exception_to_error_payload(exc: BaseException) -> dict[str, Any]:
    """Convert any exception into the canonical 6-code structured MCP error envelope."""
    if isinstance(exc, GFlowError):
        return exc.to_error_payload()
    if isinstance(exc, (ValueError, TypeError)):
        return {
            "ok": False,
            "error_code": ERR_INVALID_INPUT,
            "error": {
                "code": ERR_INVALID_INPUT,
                "message": str(exc),
                "retriable": False,
            },
        }
    return {
        "ok": False,
        "error_code": ERR_UPSTREAM_ERROR,
        "error": {
            "code": ERR_UPSTREAM_ERROR,
            "message": f"{type(exc).__name__}: {exc}",
            "retriable": True,
        },
    }


def structured_tool(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Wrap an async MCP tool so every failure raises a JSON-structured ``ToolError``."""

    @functools.wraps(fn)
    async def _wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            payload = exception_to_error_payload(exc)
            raise ToolError(json.dumps(payload, ensure_ascii=False)) from None

    return _wrapper
