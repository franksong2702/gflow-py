"""Structured exceptions and canonical MCP error codes for gflow-py."""

from __future__ import annotations

from typing import Any

# Canonical machine-readable error codes (aligned with remote MCP connector spec)
ERR_AUTH_EXPIRED = "AUTH_EXPIRED"
ERR_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
ERR_INVALID_INPUT = "INVALID_INPUT"
ERR_RATE_LIMITED = "RATE_LIMITED"
ERR_UPSTREAM_ERROR = "UPSTREAM_ERROR"
ERR_NOT_FOUND = "NOT_FOUND"


class GFlowError(Exception):
    """Base exception for all gflow-py errors with structured MCP error metadata."""

    error_code: str = ERR_UPSTREAM_ERROR
    retriable: bool = True

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        retriable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        if retriable is not None:
            self.retriable = retriable
        self.details: dict[str, Any] = details or {}

    def to_error_payload(self) -> dict[str, Any]:
        error_obj: dict[str, Any] = {
            "code": self.error_code,
            "message": str(self),
            "retriable": self.retriable,
        }
        if self.details:
            error_obj["details"] = self.details
        return {
            "ok": False,
            "error_code": self.error_code,
            "error": error_obj,
        }


class AuthError(GFlowError):
    """Google session is missing, expired, and automatic renewal could not recover it."""

    error_code = ERR_AUTH_EXPIRED
    retriable = False


class QuotaExceededError(GFlowError):
    """Google Flow AI credits or account quota are exhausted."""

    error_code = ERR_QUOTA_EXCEEDED
    retriable = False


class ValidationError(GFlowError):
    """Caller supplied invalid parameters, unsupported media format/size, or policy-blocked prompt."""

    error_code = ERR_INVALID_INPUT
    retriable = False


class NotFoundError(GFlowError):
    """Requested project, scene, job_id, or media_id does not exist."""

    error_code = ERR_NOT_FOUND
    retriable = False


class RateLimitedError(GFlowError):
    """Too many concurrent or rapid requests; back off and retry."""

    error_code = ERR_RATE_LIMITED
    retriable = True


class RpcError(GFlowError):
    """An upstream Google Flow batchexecute RPC failed."""

    error_code = ERR_UPSTREAM_ERROR
    retriable = True

    def __init__(
        self,
        message: str,
        *,
        rpc_id: str | None = None,
        code: int | None = None,
        error_code: str | None = None,
        retriable: bool | None = None,
    ) -> None:
        details: dict[str, Any] = {}
        if rpc_id is not None:
            details["rpc_id"] = rpc_id
        if code is not None:
            details["upstream_code"] = code
        super().__init__(
            message,
            error_code=error_code,
            retriable=retriable,
            details=details,
        )
        self.rpc_id = rpc_id
        self.code = code


class GenerationError(GFlowError):
    """Media generation or Scene Builder export failed."""

    error_code = ERR_UPSTREAM_ERROR
    retriable = True
