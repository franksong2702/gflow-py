"""Stateless HMAC-SHA256 signed URLs and single-use upload JTI store for HTTP MCP."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DOWNLOAD_TTL_SECONDS = 86400  # 24 hours
DEFAULT_UPLOAD_TTL_SECONDS = 1800  # 30 minutes


class FileLinkError(ValueError):
    """Raised when a signed download or upload token is malformed, expired, or forged."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((text + pad).encode("ascii"))
    except Exception as exc:
        raise FileLinkError("Malformed base64url segment") from exc


def load_or_create_signing_secret(secret_path: Path) -> bytes:
    """Load or atomically generate a 32-byte HMAC secret (`0600`) so 24h links survive restarts."""
    if secret_path.exists():
        try:
            raw = secret_path.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
        except OSError:
            pass
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    tmp = secret_path.with_suffix(f".tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    os.replace(tmp, secret_path)
    return secret


class FileLinkSigner:
    """Mint and verify HMAC-SHA256 capability tokens for ``/files/{dl,ul}/{token}``."""

    def __init__(self, secret: bytes, *, default_ttl: int = DEFAULT_DOWNLOAD_TTL_SECONDS) -> None:
        if len(secret) < 16:
            raise ValueError("FileLinkSigner secret must be at least 16 bytes.")
        self.__secret = bytes(secret)
        self.default_ttl = default_ttl

    @classmethod
    def from_persistent_store(
        cls,
        secret_path: Path,
        *,
        default_ttl: int = DEFAULT_DOWNLOAD_TTL_SECONDS,
    ) -> FileLinkSigner:
        return cls(load_or_create_signing_secret(secret_path), default_ttl=default_ttl)

    def sign(
        self,
        op: str,
        claims: dict[str, Any],
        *,
        ttl: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        now = int(time.time())
        exp = now + (ttl if ttl is not None else self.default_ttl)
        payload: dict[str, Any] = {
            **claims,
            "op": op,
            "iat": now,
            "exp": exp,
        }
        if op == "ul" and "jti" not in payload:
            payload["jti"] = secrets.token_urlsafe(16)
        body = _b64u_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        sig = _b64u_encode(hmac.new(self.__secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{sig}", payload

    def verify(self, token: str, *, op: str) -> dict[str, Any]:
        if "." not in token or token.count(".") != 1:
            raise FileLinkError("Invalid token format")
        body, sig = token.split(".", 1)
        expected_sig = _b64u_encode(
            hmac.new(self.__secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected_sig):
            raise FileLinkError("Invalid token signature")
        try:
            payload = json.loads(_b64u_decode(body).decode("utf-8"))
        except Exception as exc:
            raise FileLinkError("Invalid token payload") from exc
        if not isinstance(payload, dict):
            raise FileLinkError("Token payload must be an object")
        if payload.get("op") != op:
            raise FileLinkError("Token operation mismatch")
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or time.time() > exp:
            raise FileLinkError("Token has expired")
        return payload

    def __repr__(self) -> str:
        return "FileLinkSigner(secret=<redacted>)"


class JtiStore:
    """In-memory single-use upload tracker + upload completion metadata lookup."""

    def __init__(self, max_entries: int = 1024) -> None:
        self._max_entries = max_entries
        self._in_progress: set[str] = set()
        self._committed: dict[str, tuple[float, dict[str, Any]]] = {}
        self._created: dict[str, tuple[float, dict[str, Any]]] = {}

    def _prune(self) -> None:
        now = time.time()
        for jti in [k for k, (exp, _) in self._committed.items() if exp < now]:
            self._committed.pop(jti, None)
        for jti in [k for k, (exp, _) in self._created.items() if exp < now]:
            self._created.pop(jti, None)

    def register_issued(self, jti: str, exp: float, meta: dict[str, Any]) -> None:
        self._prune()
        if len(self._created) >= self._max_entries:
            oldest = min(self._created, key=lambda k: self._created[k][0])
            self._created.pop(oldest, None)
        self._created[jti] = (exp, meta)

    def try_begin(self, jti: str) -> bool:
        self._prune()
        if jti in self._in_progress or jti in self._committed:
            return False
        self._in_progress.add(jti)
        return True

    def commit(self, jti: str, exp: float, result: dict[str, Any]) -> None:
        self._in_progress.discard(jti)
        self._prune()
        if len(self._committed) >= self._max_entries:
            oldest = min(self._committed, key=lambda k: self._committed[k][0])
            self._committed.pop(oldest, None)
        self._committed[jti] = (exp, result)

    def rollback(self, jti: str) -> None:
        self._in_progress.discard(jti)

    def status(self, jti: str) -> dict[str, Any]:
        self._prune()
        if jti in self._committed:
            _, res = self._committed[jti]
            return {"upload_id": jti, "status": "completed", **res}
        if jti in self._in_progress:
            return {"upload_id": jti, "status": "uploading"}
        if jti in self._created:
            exp, meta = self._created[jti]
            return {
                "upload_id": jti,
                "status": "awaiting_user_upload",
                "expires_at_epoch": int(exp),
                **meta,
            }
        return {"upload_id": jti, "status": "unknown_or_expired"}


def _epoch_to_iso(sec: int | float) -> str:
    return datetime.fromtimestamp(float(sec), tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class FileTransferConfig:
    """Configuration for out-of-band signed HTTP media downloads and VM/browser uploads."""

    signer: FileLinkSigner
    base_url: str
    download_ttl_seconds: int = DEFAULT_DOWNLOAD_TTL_SECONDS
    jti_store: JtiStore = field(default_factory=JtiStore)

    def download_link_for_media(self, media_id: str, *, ttl: int | None = None) -> dict[str, Any]:
        effective_ttl = ttl if ttl is not None else self.download_ttl_seconds
        token, payload = self.signer.sign(
            "dl",
            {"kind": "media", "media_id": media_id},
            ttl=effective_ttl,
        )
        view_url = f"{self.base_url.rstrip('/')}/files/dl/{token}"
        return {
            "browser_url": view_url,
            "download_link": f"{view_url}?download=1",
            "download_expires_at": _epoch_to_iso(payload["exp"]),
            "download_ttl_seconds": effective_ttl,
        }

    def download_url_for_media(self, media_id: str, *, ttl: int | None = None) -> str:
        return self.download_link_for_media(media_id, ttl=ttl)["browser_url"]

    def download_link_for_export(
        self,
        export_filename: str,
        *,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        effective_ttl = ttl if ttl is not None else self.download_ttl_seconds
        token, payload = self.signer.sign(
            "dl",
            {"kind": "export", "filename": export_filename},
            ttl=effective_ttl,
        )
        view_url = f"{self.base_url.rstrip('/')}/files/dl/{token}"
        return {
            "browser_url": view_url,
            "download_link": f"{view_url}?download=1",
            "download_expires_at": _epoch_to_iso(payload["exp"]),
            "download_ttl_seconds": effective_ttl,
        }

    def download_url_for_export(self, export_filename: str, *, ttl: int | None = None) -> str:
        return self.download_link_for_export(export_filename, ttl=ttl)["browser_url"]

    def create_upload_link(
        self,
        project_id: str,
        *,
        label: str = "Reference image",
        ttl: int = DEFAULT_UPLOAD_TTL_SECONDS,
    ) -> dict[str, Any]:
        token, payload = self.signer.sign(
            "ul",
            {"project_id": project_id, "label": label},
            ttl=ttl,
        )
        jti = str(payload["jti"])
        url = f"{self.base_url.rstrip('/')}/files/ul/{token}"
        self.jti_store.register_issued(
            jti,
            float(payload["exp"]),
            {"project_id": project_id, "label": label, "upload_url": url},
        )
        return {
            "upload_id": jti,
            "upload_url": url,
            "project_id": project_id,
            "expires_in_seconds": ttl,
            "expires_at": _epoch_to_iso(payload["exp"]),
            "max_upload_bytes": 20 * 1024 * 1024,
            "vm_direct_put_example": (
                f'curl -fsSL -X PUT --data-binary @frame.png "{url}?filename=frame.png" '
                '-H "Content-Type: image/png"'
            ),
        }
