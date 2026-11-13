"""Async Boq `batchexecute` HTTP client for `https://flow.google.com` with auto-renewal & error classification."""

from __future__ import annotations

import base64
import json
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .exceptions import (
    ERR_INVALID_INPUT,
    ERR_NOT_FOUND,
    ERR_QUOTA_EXCEEDED,
    ERR_RATE_LIMITED,
    ERR_UPSTREAM_ERROR,
    AuthError,
    NotFoundError,
    QuotaExceededError,
    RateLimitedError,
    RpcError,
    ValidationError,
)

FLOW_BASE_URL = "https://flow.google.com"
BATCHEXECUTE_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AuthRecoveryHook = Callable[[], Awaitable[httpx.Cookies]]


@dataclass
class WizSession:
    xsrf_token: str  # WIZ_global_data.SNlM0e ("at=...")
    session_id: str  # WIZ_global_data.FdrFJe ("f.sid=...")
    build_label: str  # WIZ_global_data.cfb2h ("bl=...")
    recaptcha_sitekey: str | None = None  # WIZ_global_data.xZbWve


# Exhaustive `google.internal.labs.aisandbox.proto.common.v1.PublicAitkError` (`_.pta`) enum table
# extracted directly from Google Flow's production Boq/Angular bundle (`flow.google.com`).
PUBLIC_ERROR_CODE_MAP: dict[int, str] = {
    0: "PUBLIC_ERROR_UNSPECIFIED",
    1: "PUBLIC_ERROR_UNSAFE_GENERATION",
    2: "PUBLIC_ERROR_USER_QUOTA_REACHED",
    3: "PUBLIC_ERROR_USER_REQUESTS_THROTTLED",
    4: "PUBLIC_ERROR_HIGH_TRAFFIC",
    5: "PUBLIC_ERROR_MODEL_DISABLED_DUE_TO_TRAFFIC",
    7: "PUBLIC_ERROR_NON_ENGLISH_PROMPT",
    8: "PUBLIC_ERROR_UNSAFE_IMAGE_UPLOAD",
    9: "PUBLIC_ERROR_GENERATION_ALREADY_IN_PROGRESS",
    11: "PUBLIC_ERROR_MODEL_ACCESS_DENIED",
    12: "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED",
    14: "PUBLIC_ERROR_VIOLENCE_FILTER",
    15: "PUBLIC_ERROR_DANGER_FILTER",
    16: "PUBLIC_ERROR_SEXUAL",
    17: "PUBLIC_ERROR_MINOR_UPLOAD",
    18: "PUBLIC_ERROR_SEXUAL_UPLOAD",
    19: "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD",
    20: "PUBLIC_ERROR_PHOTOREAL_UPLOAD",
    21: "PUBLIC_ERROR_MINOR",
    22: "PUBLIC_ERROR_MINOR_HARM_UPLOAD",
    23: "PUBLIC_ERROR_PHOTOREAL_INPUT_IMAGE",
    24: "PUBLIC_ERROR_PROMINENT_PEOPLE_INPUT_IMAGE",
    25: "PUBLIC_ERROR_MINOR_INPUT_IMAGE",
    26: "PUBLIC_ERROR_UNKNOWN_IMAGE_FILE_FORMAT",
    27: "PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT",
    28: "PUBLIC_ERROR_MODEL_OVERLOADED",
    29: "PUBLIC_ERROR_HANDLE_INVALID",
    30: "PUBLIC_ERROR_HANDLE_TAKEN",
    31: "PUBLIC_ERROR_IMAGE_TOO_LARGE",
    32: "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED",
    33: "PUBLIC_ERROR_AUDIO_FILTERED",
    34: "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED_UPGRADEABLE",
    35: "PUBLIC_ERROR_SOMETHING_WENT_WRONG",
    36: "PUBLIC_ERROR_COLLECTION_INVALID",
    39: "PUBLIC_ERROR_COLLECTION_FULL",
    40: "PUBLIC_ERROR_COLLECTION_DEPTH_EXCEEDED",
    41: "PUBLIC_ERROR_IP_INPUT_IMAGE",
    42: "PUBLIC_ERROR_APP_UNAVAILABLE",
    43: "PUBLIC_BOTTLE_CONTENT_WARNING",
    44: "PUBLIC_ERROR_IMAGE_TOO_SMALL",
    45: "PUBLIC_ERROR_USER_REGION_DISALLOWED",
    46: "PUBLIC_ERROR_USER_MINOR",
    47: "PUBLIC_ERROR_IMAGE_OUTPUT_IP_FILTER",
    48: "PUBLIC_ERROR_WORKSPACE_ACCOUNT_QUOTA_REACHED",
    49: "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
    50: "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC",
    51: "PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED",
    52: "PUBLIC_ERROR_MEDIA_GENERATION_CANNOT_BE_CANCELED",
    53: "PUBLIC_ERROR_UNSAFE_VIDEO_UPLOAD",
    54: "PUBLIC_ERROR_VIDEO_UPLOAD_TIMEOUT",
    55: "PUBLIC_ERROR_VIDEO_DURATION_TOO_LONG",
    56: "PUBLIC_ERROR_APPLET_SIZE_LIMIT_EXCEEDED",
    57: "PUBLIC_ERROR_APPLET_STORAGE_QUOTA_EXCEEDED",
    58: "PUBLIC_ERROR_VIDEO_UPLOAD_FAILED",
    59: "PUBLIC_ERROR_SPEECH_EDIT",
    60: "PUBLIC_ERROR_VIDEO_EDIT",
    61: "PUBLIC_ERROR_REPUTATIONAL",
    63: "PUBLIC_ERROR_UNDERSPECIFIED_ANIMAL",
    64: "PUBLIC_ERROR_RESPONSE_TOO_LONG",
    65: "PUBLIC_ERROR_PROJECT_NOT_ALLOWLISTED",
    66: "PUBLIC_ERROR_USER_ROLE_NOT_GRANTED",
    67: "PUBLIC_ERROR_PROJECT_FLOW_ENTERPRISE_NOT_ENABLED",
    68: "PUBLIC_ERROR_VERTEX_SAFETY_FILTER_TRIGGERED",
    69: "PUBLIC_ERROR_VERTEX_UNSUPPORTED_INPUT",
    70: "PUBLIC_ERROR_VERTEX_QUOTA_EXHAUSTED",
    71: "PUBLIC_ERROR_VERTEX_QUOTA_EXHAUSTED_ADMIN",
    72: "PUBLIC_ERROR_VERTEX_SERVICE_UNAVAILABLE",
    73: "PUBLIC_ERROR_UNSAFE_SEARCH",
    74: "PUBLIC_ERROR_IAM_POLICY_DENIED",
    75: "PUBLIC_ERROR_PUBLISH_TIME_LIMIT_REACHED",
    76: "PUBLIC_ERROR_USER_BANNED",
}

_QUOTA_PUBLIC_CODES: frozenset[int] = frozenset({2, 32, 34, 48, 57, 70, 71})
_RATE_LIMIT_PUBLIC_CODES: frozenset[int] = frozenset({3, 4, 5, 9, 28, 49, 50, 51})
_UPSTREAM_PUBLIC_CODES: frozenset[int] = frozenset({0, 27, 35, 42, 54, 58, 72})
_INVALID_INPUT_PUBLIC_CODES: frozenset[int] = frozenset(
    set(PUBLIC_ERROR_CODE_MAP) - _QUOTA_PUBLIC_CODES - _RATE_LIMIT_PUBLIC_CODES - _UPSTREAM_PUBLIC_CODES
)


def _decode_proto_field1_varint(raw: bytes) -> int | None:
    """Decode protobuf wire field 1 varint (`tag = 0x08`) from `PublicAitkError` bytes."""
    idx = 0
    n = len(raw)
    while idx < n:
        tag = raw[idx]
        idx += 1
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val = 0
            shift = 0
            while idx < n:
                b = raw[idx]
                idx += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if field_num == 1:
                return val
        elif wire_type == 2:
            length = 0
            shift = 0
            while idx < n:
                b = raw[idx]
                idx += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            idx += length
        else:
            break
    return None


def _decode_proto_field1_string(raw: bytes) -> str | None:
    """Decode protobuf wire field 1 string (`tag = 0x0a`) from `google.rpc.ErrorInfo` bytes."""
    idx = 0
    n = len(raw)
    while idx < n:
        tag = raw[idx]
        idx += 1
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 2:
            length = 0
            shift = 0
            while idx < n:
                b = raw[idx]
                idx += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            chunk = raw[idx : idx + length]
            idx += length
            if field_num == 1:
                return chunk.decode("utf-8", errors="replace")
        elif wire_type == 0:
            while idx < n and (raw[idx] & 0x80):
                idx += 1
            idx += 1
        else:
            break
    return None


def _extract_boq_error_signals(detail: Any) -> tuple[int | None, str | None, str | None]:
    """Extract `(public_error_code, public_error_name, error_info_reason)` from a Boq Status JSPB tree."""
    pub_code: int | None = None
    pub_name: str | None = None
    reason: str | None = None

    def _walk(node: Any) -> None:
        nonlocal pub_code, pub_name, reason
        if not isinstance(node, list):
            return
        if len(node) >= 2 and isinstance(node[0], str):
            type_url = node[0]
            val = node[1]
            if "PublicAitkError" in type_url:
                if isinstance(val, list) and val and isinstance(val[0], int):
                    pub_code = val[0]
                    pub_name = PUBLIC_ERROR_CODE_MAP.get(pub_code, f"PUBLIC_ERROR_{pub_code}")
                elif isinstance(val, str) and val:
                    padded = val + "=" * (-len(val) % 4)
                    try:
                        raw_b = base64.b64decode(padded)
                        decoded = _decode_proto_field1_varint(raw_b)
                        if decoded is not None:
                            pub_code = decoded
                            pub_name = PUBLIC_ERROR_CODE_MAP.get(decoded, f"PUBLIC_ERROR_{decoded}")
                    except Exception:
                        pass
            elif "google.rpc.ErrorInfo" in type_url:
                if isinstance(val, list) and val and isinstance(val[0], str):
                    reason = val[0]
                elif isinstance(val, str) and val:
                    padded = val + "=" * (-len(val) % 4)
                    try:
                        raw_b = base64.b64decode(padded)
                        decoded_str = _decode_proto_field1_string(raw_b)
                        if decoded_str:
                            reason = decoded_str
                    except Exception:
                        pass
        for child in node:
            if isinstance(child, list):
                _walk(child)

    _walk(detail)
    return pub_code, pub_name, reason


def _classify_rpc_error(rpc_id: str, code: int | None, detail: Any) -> Exception:
    """Map upstream Boq/gRPC status codes and `PublicAitkError` wire payloads to canonical gflow exceptions."""
    pub_code, pub_name, err_reason = _extract_boq_error_signals(detail)
    base_details: dict[str, Any] = {"rpc_id": rpc_id}
    if code is not None:
        base_details["upstream_code"] = code
    if pub_name is not None:
        base_details["public_error"] = pub_name
        base_details["public_error_code"] = pub_code
    if err_reason:
        base_details["error_reason"] = err_reason

    parts = [str(detail)]
    if pub_name:
        parts.insert(0, pub_name)
    if err_reason and err_reason not in parts[0]:
        parts.insert(0, err_reason)
    text = " | ".join(parts)
    low = text.lower()

    # 1. Decode explicit Google Flow `PublicAitkError` (`_.pta`) first (authoritative when present)
    if pub_code is not None:
        if pub_code in _QUOTA_PUBLIC_CODES:
            return QuotaExceededError(
                f"Google Flow quota or AI credits exhausted ({rpc_id}, {pub_name}): {detail}",
                details=base_details,
            )
        if pub_code in _RATE_LIMIT_PUBLIC_CODES:
            return RateLimitedError(
                f"Google Flow rate or concurrency limit reached ({rpc_id}, {pub_name}): {detail}",
                details=base_details,
            )
        if pub_code in _INVALID_INPUT_PUBLIC_CODES:
            return ValidationError(
                f"Invalid input or policy-rejected request on {rpc_id} ({pub_name}): {detail}",
                error_code=ERR_INVALID_INPUT,
                retriable=False,
                details=base_details,
            )

    # 2. Canonical gRPC status codes inside Boq `wrb.fr` error frames:
    #    3 = INVALID_ARGUMENT, 5 = NOT_FOUND, 7 = PERMISSION_DENIED,
    #    8 = RESOURCE_EXHAUSTED, 14 = UNAVAILABLE, 16 = UNAUTHENTICATED
    if code == 16 or "unauthenticated" in low:
        return AuthError(
            f"AUTH_EXPIRED: Google Flow rejected session credentials on RPC {rpc_id} (code=16). "
            "Run 'gflow login' on the server (M15).",
            details=base_details,
        )
    if code == 5 or "not_found" in low or "not found" in low:
        return NotFoundError(
            f"Requested Google Flow resource not found ({rpc_id}): {text}",
            details=base_details,
        )
    if code == 8:
        if any(
            k in low
            for k in (
                "rate",
                "too many",
                "throttle",
                "concurrent_limit",
                "too_much_traffic",
                "overloaded",
            )
        ):
            return RateLimitedError(
                f"Google Flow rate/concurrency limit reached ({rpc_id}): {text}",
                details=base_details,
            )
        if any(k in low for k in ("credit", "quota", "balance", "insufficient", "paygate", "billing")):
            return QuotaExceededError(
                f"Insufficient Google Flow AI credits or quota exhausted ({rpc_id}): {text}",
                details=base_details,
            )
        return QuotaExceededError(
            f"Google Flow quota or credit limit reached ({rpc_id}, code=8): {text}",
            details=base_details,
        )
    if any(
        k in low
        for k in (
            "concurrent_limit",
            "requests_throttled",
            "stream_error_rate_limit",
            "too_much_traffic",
        )
    ):
        return RateLimitedError(
            f"Google Flow rate/concurrency limit reached ({rpc_id}): {text}",
            details=base_details,
        )
    if any(k in low for k in ("insufficient credit", "not enough credit", "out of credits", "quota")):
        return QuotaExceededError(
            f"Google Flow credits or quota exhausted ({rpc_id}): {text}",
            details=base_details,
        )
    if code == 3 or any(
        k in low
        for k in (
            "invalid_argument",
            "safety",
            "policy",
            "violat",
            "prohibited",
            "blocked",
            "unsupported",
            "unsafe_generation",
            "danger_filter",
            "prominent_people",
            "non_english_prompt",
        )
    ):
        return ValidationError(
            f"Invalid input or policy-rejected prompt on {rpc_id}: {text}",
            error_code=ERR_INVALID_INPUT,
            retriable=False,
            details=base_details,
        )
    err = RpcError(
        f"Google Flow RPC {rpc_id} returned error: {text}",
        rpc_id=rpc_id,
        code=code,
        error_code=ERR_UPSTREAM_ERROR,
        retriable=True,
    )
    err.details.update(base_details)
    return err


class FlowHttpRpc:
    """Pure-HTTP Boq `batchexecute` transport backed by Google session cookies."""

    def __init__(
        self,
        cookies: httpx.Cookies,
        *,
        timeout: float = 45.0,
        user_agent: str = _DEFAULT_UA,
        on_auth_expired: AuthRecoveryHook | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._on_auth_expired = on_auth_expired
        self._cookies = cookies
        self._client = self._build_http_client(cookies)
        self._wiz: WizSession | None = None
        self._req_counter = random.randint(10000, 90000)

    def _build_http_client(self, cookies: httpx.Cookies) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=self._timeout,
            headers={
                "User-Agent": self._user_agent,
                "Origin": FLOW_BASE_URL,
                "Referer": f"{FLOW_BASE_URL}/",
            },
        )

    async def replace_cookies(self, cookies: httpx.Cookies) -> None:
        """Hot-swap the underlying HTTP cookie jar (after keepalive rotation or recovery)."""
        old = self._client
        self._cookies = cookies
        self._client = self._build_http_client(cookies)
        self._wiz = None
        await old.aclose()

    @property
    def wiz(self) -> WizSession | None:
        return self._wiz

    async def close(self) -> None:
        await self._client.aclose()

    async def bootstrap(self, *, force: bool = False, _allow_recover: bool = True) -> WizSession:
        if self._wiz is not None and not force:
            return self._wiz

        try:
            resp = await self._client.get(f"{FLOW_BASE_URL}/?pli=1")
        except httpx.HTTPError as exc:
            raise RpcError(
                f"Network error connecting to Google Flow: {exc}",
                error_code=ERR_UPSTREAM_ERROR,
                retriable=True,
            ) from exc

        url_str = str(resp.url)
        html = resp.text
        m_at = re.search(r'"SNlM0e":"([^"]+)"', html)
        m_sid = re.search(r'"FdrFJe":"([^"]+)"', html)
        m_bl = re.search(r'"cfb2h":"([^"]+)"', html)
        m_rc = re.search(r'"xZbWve":"([^"]+)"', html)

        session_dead = (
            "accounts.google.com" in url_str
            or "ServiceLogin" in url_str
            or not m_at
            or not m_bl
        )
        if session_dead:
            if _allow_recover and self._on_auth_expired is not None:
                recovered_jar = await self._on_auth_expired()
                await self.replace_cookies(recovered_jar)
                return await self.bootstrap(force=True, _allow_recover=False)
            raise AuthError(
                "AUTH_EXPIRED: Google session is not authenticated or has expired, "
                "and automatic renewal did not succeed. Run 'gflow login' on the server."
            )

        assert m_at is not None and m_bl is not None
        self._wiz = WizSession(
            xsrf_token=m_at.group(1),
            session_id=m_sid.group(1) if m_sid else "",
            build_label=m_bl.group(1),
            recaptcha_sitekey=m_rc.group(1) if m_rc else None,
        )
        return self._wiz

    async def call(
        self,
        rpc_id: str,
        payload: Any,
        *,
        source_path: str = "/",
        retry_on_xsrf: bool = True,
    ) -> Any:
        wiz = await self.bootstrap()
        self._req_counter += 100000

        inner_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        f_req = json.dumps(
            [[[rpc_id, inner_json, None, "generic"]]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        params = {
            "rpcids": rpc_id,
            "source-path": source_path,
            "bl": wiz.build_label,
            "f.sid": wiz.session_id,
            "hl": "en",
            "_reqid": str(self._req_counter),
            "rt": "c",
        }
        data = {
            "f.req": f_req,
            "at": wiz.xsrf_token,
        }
        try:
            resp = await self._client.post(
                f"{FLOW_BASE_URL}{BATCHEXECUTE_PATH}",
                params=params,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            )
        except httpx.HTTPError as exc:
            raise RpcError(
                f"Network error calling Google Flow RPC {rpc_id}: {exc}",
                rpc_id=rpc_id,
                error_code=ERR_UPSTREAM_ERROR,
                retriable=True,
            ) from exc

        if resp.status_code in (401, 403) and retry_on_xsrf:
            await self.bootstrap(force=True)
            return await self.call(
                rpc_id,
                payload,
                source_path=source_path,
                retry_on_xsrf=False,
            )
        if resp.status_code == 401:
            if self._on_auth_expired is not None:
                recovered_jar = await self._on_auth_expired()
                await self.replace_cookies(recovered_jar)
                await self.bootstrap(force=True, _allow_recover=False)
                return await self.call(
                    rpc_id,
                    payload,
                    source_path=source_path,
                    retry_on_xsrf=False,
                )
            raise AuthError(
                "AUTH_EXPIRED: Google Flow rejected session credentials (HTTP 401). Run 'gflow login'."
            )
        if resp.status_code == 429:
            raise RateLimitedError(f"Google Flow rate-limited RPC {rpc_id} (HTTP 429).")
        if resp.status_code == 404:
            raise NotFoundError(f"Google Flow endpoint/resource not found for {rpc_id} (HTTP 404).")
        if resp.status_code >= 400:
            raise RpcError(
                f"Google Flow RPC {rpc_id} failed with HTTP {resp.status_code}: {resp.text[:300]}",
                rpc_id=rpc_id,
                code=resp.status_code,
                error_code=ERR_INVALID_INPUT if resp.status_code == 400 else ERR_UPSTREAM_ERROR,
                retriable=resp.status_code >= 500,
            )
        try:
            return self._parse_batchexecute(rpc_id, resp.text)
        except AuthError:
            if retry_on_xsrf and self._on_auth_expired is not None:
                recovered_jar = await self._on_auth_expired()
                await self.replace_cookies(recovered_jar)
                await self.bootstrap(force=True, _allow_recover=False)
                return await self.call(
                    rpc_id,
                    payload,
                    source_path=source_path,
                    retry_on_xsrf=False,
                )
            raise

    async def download_bytes(self, url: str) -> tuple[bytes, str | None]:
        try:
            resp = await self._client.get(url)
            if resp.status_code == 404:
                raise NotFoundError(f"Media URL returned HTTP 404: {url[:120]}")
            if resp.status_code == 429:
                raise RateLimitedError("Rate-limited while downloading media bytes from Google.")
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type")
        except (NotFoundError, RateLimitedError):
            raise
        except httpx.HTTPError as exc:
            raise RpcError(
                f"Failed downloading media asset from Google Flow: {exc}",
                error_code=ERR_UPSTREAM_ERROR,
                retriable=True,
            ) from exc

    @staticmethod
    def _parse_batchexecute(rpc_id: str, body: str) -> Any:
        text = body.lstrip()
        if text.startswith(")]}'"):
            text = text[4:].lstrip()

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("["):
                continue
            try:
                envelope = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(envelope, list):
                continue
            for frame in envelope:
                if not isinstance(frame, list) or len(frame) < 3:
                    continue
                if frame[0] == "wrb.fr" and frame[1] == rpc_id:
                    payload_str = frame[2]
                    if payload_str is None:
                        err_detail = frame[5] if len(frame) > 5 else frame
                        code = None
                        if (
                            isinstance(err_detail, list)
                            and err_detail
                            and isinstance(err_detail[0], int)
                        ):
                            code = err_detail[0]
                        raise _classify_rpc_error(rpc_id, code, err_detail)
                    return json.loads(payload_str)
        raise RpcError(
            f"Malformed batchexecute response for {rpc_id}: {body[:300]}",
            rpc_id=rpc_id,
        )
