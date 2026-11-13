"""FastMCP server and production-hardened tool definitions for Google Flow (`gflow-mcp`)."""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from .._proto import (
    resolve_image_aspect,
    resolve_image_model_key,
    resolve_video_aspect,
    resolve_video_model_key,
    resolve_video_resolution,
)
from ..auth import DEFAULT_KEEPALIVE_INTERVAL_SECONDS
from ..client import FlowClient
from ..exceptions import NotFoundError, ValidationError
from ..media_probe import measure_mp4_dimensions
from ..paths import get_active_profile, load_context, save_context
from ..types import MediaItem
from ._errors import structured_tool
from ._filelink import DEFAULT_UPLOAD_TTL_SECONDS, FileTransferConfig
from ._jobs import JobStore

__all__ = [
    "MAX_INLINE_IMAGE_BYTES",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "create_server",
]

SERVER_NAME = "gflow"
MAX_INLINE_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MiB decoded bytes for inline JSON-RPC base64
MAX_URL_FETCH_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB (`20,971,520` bytes) exact Google Flow image cap
DEFAULT_SOFT_WAIT_SECONDS = 25.0
MAX_SOFT_WAIT_SECONDS = 35.0  # Stay safely under 45s remote client HTTP timeout


def _params_fingerprint(params: dict[str, Any] | None) -> str | None:
    """Stable SHA-256 of the output-affecting tool parameters (``None`` when no params are supplied).

    Used so that reusing an ``idempotency_key`` with changed parameters (e.g. 16:9 -> 9:16) is rejected
    instead of silently replaying the old job.
    """
    if params is None:
        return None
    blob = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


SERVER_INSTRUCTIONS = (
    "Drive Google Flow (flow.google.com) over MCP.\n"
    "Key Remote & Production Semantics:\n"
    "1. Remote Image Upload (no human needed): Call `flow_upload_image(image_base64=..., file_name=...)` "
    "(<= 15 MiB / 15,728,640 decoded bytes; PNG/JPEG/WebP) OR `flow_create_upload_link` + HTTP PUT raw bytes "
    "(<= 20 MiB / 20,971,520 bytes, 30m / 1,800s TTL) to obtain a `media_id` for `start_frame_media_id` / "
    "`end_frame_media_id` / `reference_media_ids`.\n"
    "2. Direct Asset Download: When HTTP file transfer is active (always enabled on HTTPS OAuth deployments), "
    "generated media, `flow_get_media`, and `flow_scene_export_mp4` return `download_link` (unauthenticated "
    "HMAC-SHA256 HTTPS URL, 24h / 86,400s TTL, with `X-GFlow-Bytes` & `X-GFlow-SHA256` headers). "
    "Scene export MP4s and job records are retained on server disk for 7 days (604,800s).\n"
    "3. Timeout & Idempotency: Generation/export tools accept `idempotency_key` (deduplicated for 7 days; "
    "never double-submits or double-charges credits) and return `job_id`. Even with `wait=True`, server soft-waits "
    "at most `wait_timeout_seconds` (default 25s, max 35s) and returns `status='running'` + `job_id` before "
    "client HTTP disconnect; poll completion with `flow_poll_media(job_id=...)`.\n"
    "4. Session Auto-Renewal & Errors: Background keepalive rotates Google cookies via `RotateCookies` every "
    "9 minutes (540s) with 3-rung reactive recovery; if unrecoverable, tools fail fast with `AUTH_EXPIRED` "
    "instructing the operator to run `gflow login` on the server host (verifiable via `flow_account_usage(profile='expired-test')`). "
    "All tool failures return structured JSON with `error_code` in "
    "{`AUTH_EXPIRED`, `QUOTA_EXCEEDED`, `INVALID_INPUT`, `RATE_LIMITED`, `UPSTREAM_ERROR`, `NOT_FOUND`}.\n"
    "5. Verified Concurrency & Limits: Max 4 concurrent active Google Flow video renders per account "
    "(`PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED` -> `RATE_LIMITED`); batch `count` 1..4; max 3 `reference_media_ids`; "
    "max 4 concurrent `/files/ul` HTTP uploads; max 4 concurrent `/files/dl` HTTP downloads."
)

ClientFactory = Callable[[], FlowClient]


def _detect_and_validate_image_bytes(
    raw: bytes,
    *,
    file_name: str = "frame.png",
    explicit_mime: str | None = None,
    max_bytes: int = MAX_INLINE_IMAGE_BYTES,
) -> tuple[str, str]:
    """Validate image size & magic bytes (`PNG`, `JPEG`, `WebP`); return `(mime_type, normalized_filename)`."""
    if not raw:
        raise ValidationError("Uploaded image data is empty.")
    if len(raw) > max_bytes:
        limit_mib = max_bytes / (1024 * 1024)
        raise ValidationError(
            f"Image size ({len(raw)} bytes) exceeds limit ({limit_mib:.0f} MiB). "
            "Use flow_create_upload_link + HTTP PUT for images up to 20 MiB (20,971,520 bytes), or compress the image."
        )

    detected_mime: str | None = None
    default_ext = ".png"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_mime = "image/png"
        default_ext = ".png"
    elif raw.startswith(b"\xff\xd8\xff"):
        detected_mime = "image/jpeg"
        default_ext = ".jpg"
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        detected_mime = "image/webp"
        default_ext = ".webp"

    if detected_mime is None:
        raise ValidationError(
            "Unsupported image format (invalid magic bytes). Supported formats: PNG, JPEG, WebP."
        )

    if explicit_mime:
        clean_mime = explicit_mime.split(";")[0].strip().lower()
        if clean_mime not in ("image/png", "image/jpeg", "image/jpg", "image/webp"):
            raise ValidationError(
                f"Unsupported mime_type {explicit_mime!r}. Supported: image/png, image/jpeg, image/webp."
            )

    clean_name = Path(file_name.strip() or f"frame{default_ext}").name
    if "." not in clean_name:
        clean_name = f"{clean_name}{default_ext}"
    if len(clean_name) > 256:
        raise ValidationError("file_name must not exceed 256 characters.")
    return detected_mime, clean_name


def _decode_base64_image(data_b64: str) -> bytes:
    """Decode raw base64 or `data:image/...;base64,...` string into bytes."""
    cleaned = data_b64.strip()
    if not cleaned:
        raise ValidationError("image_base64 must not be empty.")
    if cleaned.startswith("data:"):
        comma = cleaned.find(",")
        if comma == -1:
            raise ValidationError("Malformed data URL in image_base64.")
        cleaned = cleaned[comma + 1 :].strip()
    # Fast pre-check before allocating huge buffer
    if len(cleaned) > int(MAX_INLINE_IMAGE_BYTES * 1.4) + 1024:
        raise ValidationError(
            f"image_base64 exceeds the {MAX_INLINE_IMAGE_BYTES // (1024 * 1024)} MiB inline limit. "
            "Use flow_create_upload_link + HTTP PUT (up to 20 MiB) instead."
        )
    try:
        return base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(f"Invalid base64 encoding in image_base64: {exc}") from exc


def _utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(
        ts if ts is not None else time.time(),
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


class _ClientManager:
    """Loop-bound lifecycle & background cookie-keepalive manager for ``FlowClient``."""

    def __init__(
        self,
        factory: ClientFactory,
        *,
        keepalive_interval: float = 0.0,
    ) -> None:
        self._factory = factory
        self._keepalive_interval = keepalive_interval
        self._client: FlowClient | None = None
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._started_at: str = _utc_iso()
        self._last_attempt_ts: float | None = None
        self._last_success_ts: float | None = None
        self._attempt_count: int = 0
        self._success_count: int = 0
        self._last_status: str = "idle" if keepalive_interval <= 0 else "starting"

    async def get(self) -> FlowClient:
        async with self._lock:
            if self._client is None:
                client = self._factory()
                await client.__aenter__()
                self._client = client
            return self._client

    async def _run_single_rotation(self) -> None:
        self._attempt_count += 1
        self._last_attempt_ts = time.time()
        try:
            client = await self.get()
            if hasattr(client, "keepalive_rotate"):
                ok = await client.keepalive_rotate()
                if ok:
                    self._success_count += 1
                    self._last_success_ts = time.time()
                    self._last_status = "ok"
                else:
                    self._last_status = "skipped_or_noop"
            else:
                self._last_status = "unsupported_mock"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_status = f"error: {type(exc).__name__}"

    def renewal_telemetry(self) -> dict[str, Any]:
        next_due = (
            _utc_iso(self._last_attempt_ts + self._keepalive_interval)
            if (self._keepalive_interval > 0 and self._last_attempt_ts is not None)
            else None
        )
        return {
            "enabled": self._keepalive_interval > 0,
            "interval_seconds": int(self._keepalive_interval),
            "mechanism": (
                "Proactive POST https://accounts.google.com/RotateCookies every 540s (9m) "
                "+ 3-rung reactive recovery (disk RotateCookies -> sibling ~/.notebooklm sync -> "
                "headless browser_profile SSO re-mint); raises AUTH_EXPIRED if all fail."
            ),
            "on_failure_error_code": "AUTH_EXPIRED",
            "on_failure_operator_action": "Run 'gflow login' on the server host",
            "started_at": self._started_at,
            "last_rotation_attempt_at": (
                _utc_iso(self._last_attempt_ts) if self._last_attempt_ts is not None else None
            ),
            "last_rotation_succeeded_at": (
                _utc_iso(self._last_success_ts) if self._last_success_ts is not None else None
            ),
            "next_scheduled_rotation_at": next_due,
            "rotation_attempt_count": self._attempt_count,
            "rotation_success_count": self._success_count,
            "last_rotation_status": self._last_status,
        }

    def start_background_keepalive(self) -> None:
        if self._keepalive_interval <= 0 or self._keepalive_task is not None:
            return

        async def _loop() -> None:
            await self._run_single_rotation()
            while True:
                await asyncio.sleep(self._keepalive_interval)
                await self._run_single_rotation()

        self._keepalive_task = asyncio.create_task(_loop())

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None

        async with self._lock:
            if self._client is not None:
                client = self._client
                self._client = None
                await client.close()


def _serialize_media(item: MediaItem, ft: FileTransferConfig | None) -> dict[str, Any]:
    data = item.to_dict()
    data["media_id"] = item.id
    if ft is not None and item.id:
        data.update(ft.download_link_for_media(item.id))
    return data


def create_server(
    *,
    profile: str | None = None,
    client_factory: ClientFactory | None = None,
    auth: AuthProvider | None = None,
    file_transfer: FileTransferConfig | None = None,
    job_store: JobStore | None = None,
    keepalive_interval: float | None = None,
) -> FastMCP:
    """Construct the ``gflow`` FastMCP server with all Google Flow tools registered."""
    resolved_profile = get_active_profile(profile)
    default_keepalive = (
        DEFAULT_KEEPALIVE_INTERVAL_SECONDS if client_factory is None else 0.0
    )
    effective_keepalive = (
        default_keepalive if keepalive_interval is None else keepalive_interval
    )
    factory = client_factory or (lambda: FlowClient.from_storage(profile=resolved_profile))
    manager = _ClientManager(factory, keepalive_interval=effective_keepalive)
    jobs = job_store or JobStore()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        manager.start_background_keepalive()
        try:
            yield {"profile": resolved_profile}
        finally:
            await manager.close()

    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        auth=auth,
    )

    if file_transfer is not None:
        from ._fileroutes import prune_expired_exports, register_file_routes

        prune_expired_exports()
        register_file_routes(mcp, file_transfer, manager.get)

    async def _poll_soft(
        client: FlowClient,
        media_ids: list[str],
        *,
        wait: bool,
        wait_timeout_seconds: float,
    ) -> tuple[str, list[MediaItem]]:
        """Poll media status; if soft timeout expires while still running, return running state cleanly."""
        bounded_timeout = max(1.0, min(float(wait_timeout_seconds), MAX_SOFT_WAIT_SECONDS))
        if not wait:
            items = await client.poll_media(media_ids, wait=False)
        else:
            deadline = time.monotonic() + bounded_timeout
            while True:
                items = await client.poll_media(media_ids, wait=False)
                if all(bool(it.download_url) for it in items):
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(3.0)
        all_done = bool(items) and all(bool(it.download_url) for it in items)
        return ("completed" if all_done else "running"), items

    async def _run_media_generation_job(
        *,
        op: str,
        idempotency_key: str | None,
        wait: bool,
        wait_timeout_seconds: float,
        submit_coro_fn: Callable[[FlowClient], Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await manager.get()

        async def _submit(_job_id: str) -> dict[str, Any]:
            submitted_items: list[MediaItem] = await submit_coro_fn(client)
            mids = [it.id for it in submitted_items if it.id]
            proj_id = submitted_items[0].project_id if submitted_items else None
            return {
                "kind": "media",
                "project_id": proj_id,
                "media_ids": mids,
            }

        record, replayed = await jobs.submit_or_reuse(
            op=op,
            idempotency_key=idempotency_key,
            submit_fn=_submit,
            params_fingerprint=_params_fingerprint(params),
        )
        media_ids: list[str] = list(record.get("media_ids") or [])
        status, items = await _poll_soft(
            client,
            media_ids,
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        await jobs.update_job(record["job_id"], {"status": status})
        return {
            "job_id": record["job_id"],
            "status": status,
            "idempotency_key": record.get("idempotency_key"),
            "idempotent_replay": replayed,
            "project_id": record.get("project_id"),
            "media_ids": media_ids,
            "items": [_serialize_media(it, file_transfer) for it in items],
        }

    # ── 0. Connector Documentation Resources (`gflow://docs/...`) ────────────

    @mcp.resource("gflow://docs/connector")
    def connector_reference_doc() -> str:
        """Canonical Google Flow MCP `CONNECTOR.md` with verified limits, concurrency caps, TTLs, auto-renewal, and full error table."""
        doc_path = Path(__file__).resolve().parents[3] / "docs" / "CONNECTOR.md"
        if doc_path.is_file():
            return doc_path.read_text(encoding="utf-8")
        return SERVER_INSTRUCTIONS

    @mcp.resource("gflow://docs/limits")
    def connector_limits_doc() -> str:
        """Detailed Google Flow MCP verified limits, retention windows, and `PublicAitkError` code mapping."""
        doc_path = Path(__file__).resolve().parents[3] / "docs" / "MCP_CONNECTOR_LIMITS.md"
        if doc_path.is_file():
            return doc_path.read_text(encoding="utf-8")
        return SERVER_INSTRUCTIONS

    # ── 1. Account & Catalog Tools ───────────────────────────────────────────

    @mcp.tool()
    @structured_tool
    async def flow_account_usage(profile: str | None = None) -> dict[str, Any]:
        """Return Google Flow account identity, live AI credits, session auto-renewal telemetry, and verified limits.

        Optional `profile`: omit for the server's active profile (`default`); pass `profile="expired-test"`
        to empirically trigger a live upstream Google `ServiceLogin` / `AUTH_EXPIRED` response without
        disrupting the active `default` session.
        """
        target_profile = (profile or "").strip() or resolved_profile
        if target_profile != resolved_profile:
            async with FlowClient.from_storage(profile=target_profile) as temp_client:
                user, credits = await asyncio.gather(
                    temp_client.whoami(),
                    temp_client.get_credits(),
                )
            ctx = load_context(target_profile)
            return {
                "profile": target_profile,
                "active_project_id": ctx.get("project_id"),
                "file_transfer_active": file_transfer is not None,
                "download_link_ttl_seconds": (
                    file_transfer.download_ttl_seconds if file_transfer is not None else None
                ),
                "user": user.to_dict(),
                "credits": credits.to_dict(),
            }

        client = await manager.get()
        user, credits = await asyncio.gather(client.whoami(), client.get_credits())
        ctx = load_context(resolved_profile)
        return {
            "profile": resolved_profile,
            "active_project_id": ctx.get("project_id"),
            "file_transfer_active": file_transfer is not None,
            "download_link_ttl_seconds": (
                file_transfer.download_ttl_seconds if file_transfer is not None else None
            ),
            "user": user.to_dict(),
            "credits": credits.to_dict(),
            "session_renewal": manager.renewal_telemetry(),
            "verified_limits": {
                "inline_base64_upload_max_bytes": MAX_INLINE_IMAGE_BYTES,
                "url_or_upload_link_max_bytes": MAX_URL_FETCH_IMAGE_BYTES,
                "upload_link_ttl_seconds": DEFAULT_UPLOAD_TTL_SECONDS,
                "download_link_ttl_seconds": (
                    file_transfer.download_ttl_seconds if file_transfer is not None else 86400
                ),
                "scene_export_retention_seconds": 604800,
                "job_idempotency_retention_seconds": 604800,
                "max_concurrent_video_generations_per_account": 4,
                "max_batch_count_per_call": 4,
                "max_reference_images_per_video": 3,
                "max_concurrent_http_uploads": 4,
                "max_concurrent_http_downloads": 4,
            },
        }

    @mcp.tool()
    @structured_tool
    async def flow_list_models(
        kind: Literal["video", "image", "audio"] | None = None,
    ) -> list[dict[str, Any]]:
        """List available Google Flow video (Veo 3.1 / Omni 1.2) and image (Nano Banana) models."""
        client = await manager.get()
        models = await client.list_models(kind=kind)
        return [m.to_dict() for m in models]

    # ── 2. Project Management Tools ──────────────────────────────────────────

    @mcp.tool()
    @structured_tool
    async def flow_project_list(limit: int = 30, profile: str | None = None) -> dict[str, Any]:
        """List recent Google Flow projects and report the active default project."""
        if limit < 1:
            raise ValidationError("limit must be >= 1.")
        target_profile = (profile or "").strip() or resolved_profile
        if target_profile != resolved_profile:
            async with FlowClient.from_storage(profile=target_profile) as temp_client:
                projects = await temp_client.list_projects(limit=limit)
            ctx = load_context(target_profile)
            return {
                "profile": target_profile,
                "active_project_id": ctx.get("project_id"),
                "count": len(projects),
                "projects": [p.to_dict() for p in projects],
            }
        client = await manager.get()
        projects = await client.list_projects(limit=limit)
        ctx = load_context(resolved_profile)
        return {
            "active_project_id": ctx.get("project_id"),
            "count": len(projects),
            "projects": [p.to_dict() for p in projects],
        }

    @mcp.tool()
    @structured_tool
    async def flow_project_create(
        title: str = "Untitled project",
        set_active: bool = True,
    ) -> dict[str, Any]:
        """Create a new Google Flow project and optionally make it the active default project."""
        client = await manager.get()
        proj = await client.create_project(title)
        if set_active:
            ctx = load_context(resolved_profile)
            ctx["project_id"] = proj.id
            ctx["project_title"] = proj.title
            save_context(ctx, resolved_profile)
        return {
            "project": proj.to_dict(),
            "active_project_id": (
                proj.id if set_active else load_context(resolved_profile).get("project_id")
            ),
        }

    @mcp.tool()
    @structured_tool
    async def flow_project_use(project_id: str) -> dict[str, Any]:
        """Set the active Google Flow project (accepts full UUID, unique prefix, or exact title)."""
        client = await manager.get()
        proj = await client.get_project(project_id)
        ctx = load_context(resolved_profile)
        ctx["project_id"] = proj.id
        ctx["project_title"] = proj.title
        save_context(ctx, resolved_profile)
        return {"active_project": proj.to_dict()}

    @mcp.tool()
    @structured_tool
    async def flow_project_rename(project_id: str, new_title: str) -> dict[str, Any]:
        """Rename an existing Google Flow project."""
        if not new_title.strip():
            raise ValidationError("new_title must not be empty.")
        client = await manager.get()
        proj = await client.rename_project(project_id, new_title)
        return proj.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_project_delete(project_id: str, confirm: bool = False) -> dict[str, Any]:
        """Delete a Google Flow project. Requires `confirm=True`; otherwise returns a preview."""
        client = await manager.get()
        proj = await client.get_project(project_id)
        if not confirm:
            return {
                "status": "needs_confirmation",
                "message": (
                    f"Re-run flow_project_delete with confirm=True to permanently delete "
                    f"'{proj.title}' ({proj.id})."
                ),
                "project": proj.to_dict(),
            }
        await client.delete_project(proj.id)
        ctx = load_context(resolved_profile)
        if ctx.get("project_id") == proj.id:
            ctx.pop("project_id", None)
            ctx.pop("project_title", None)
            save_context(ctx, resolved_profile)
        return {"status": "deleted", "project_id": proj.id, "title": proj.title}

    @mcp.tool()
    @structured_tool
    async def flow_project_contents(project_id: str | None = None) -> dict[str, Any]:
        """Inspect all workflows, generated images/videos, and Scene Builder timelines in a project."""
        client = await manager.get()
        contents = await client.get_project_contents(project_id)
        return {
            "project_id": contents.project_id,
            "workflow_count": len(contents.workflows),
            "media_count": len(contents.media),
            "scene_count": len(contents.scenes),
            "workflows": [w.to_dict() for w in contents.workflows],
            "media": [_serialize_media(m, file_transfer) for m in contents.media],
            "scenes": [s.to_dict() for s in contents.scenes],
        }

    # ── 3. Upload, Frame Capture & Media Inspection Tools ────────────────────

    @mcp.tool()
    @structured_tool
    async def flow_upload_image(
        file_path: str | None = None,
        image_base64: str | None = None,
        image_url: str | None = None,
        file_name: str = "frame.png",
        mime_type: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload an image (PNG, JPEG, or WebP) into Google Flow and return its `media_id`.

        Supply exactly ONE of:
        - `image_base64`: Remote-friendly inline base64 data (raw base64 or `data:image/...;base64,...`, max 15 MiB decoded).
        - `image_url`: HTTP/HTTPS image URL reachable by the server (max 20 MiB / 20,971,520 bytes).
        - `file_path`: Server-local filesystem path (for local M15 / stdio callers, max 20 MiB).

        The returned `media_id` can be passed directly to `flow_generate_video(start_frame_media_id=..., end_frame_media_id=..., reference_media_ids=[...])` or `flow_generate_image`.
        """
        provided = [
            name
            for name, val in (
                ("file_path", file_path),
                ("image_base64", image_base64),
                ("image_url", image_url),
            )
            if val is not None and str(val).strip()
        ]
        if len(provided) != 1:
            raise ValidationError(
                "Provide exactly one of `image_base64`, `image_url`, or `file_path`."
            )

        client = await manager.get()

        if image_base64 is not None and image_base64.strip():
            raw_bytes = _decode_base64_image(image_base64)
            detected_mime, clean_name = _detect_and_validate_image_bytes(
                raw_bytes,
                file_name=file_name,
                explicit_mime=mime_type,
                max_bytes=MAX_INLINE_IMAGE_BYTES,
            )
            item = await client.upload_image(
                project_id,
                raw_bytes,
                file_name=clean_name,
                mime_type=detected_mime,
            )
            return _serialize_media(item, file_transfer)

        if image_url is not None and image_url.strip():
            u = image_url.strip()
            if not (u.startswith("https://") or u.startswith("http://")):
                raise ValidationError("image_url must start with http:// or https://.")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
                    resp = await http.get(u)
                    resp.raise_for_status()
                    raw_bytes = resp.content
            except httpx.HTTPError as exc:
                raise ValidationError(f"Could not fetch image_url {u!r}: {exc}") from exc
            detected_mime, clean_name = _detect_and_validate_image_bytes(
                raw_bytes,
                file_name=file_name,
                explicit_mime=mime_type,
                max_bytes=MAX_URL_FETCH_IMAGE_BYTES,
            )
            item = await client.upload_image(
                project_id,
                raw_bytes,
                file_name=clean_name,
                mime_type=detected_mime,
            )
            return _serialize_media(item, file_transfer)

        assert file_path is not None
        local = Path(file_path).expanduser()
        if not local.is_file():
            raise NotFoundError(f"Local image file does not exist on server: {local}")
        raw_bytes = local.read_bytes()
        guessed, _ = mimetypes.guess_type(local.name)
        detected_mime, clean_name = _detect_and_validate_image_bytes(
            raw_bytes,
            file_name=local.name,
            explicit_mime=mime_type or guessed,
            max_bytes=MAX_URL_FETCH_IMAGE_BYTES,
        )
        item = await client.upload_image(
            project_id,
            raw_bytes,
            file_name=clean_name,
            mime_type=detected_mime,
        )
        return _serialize_media(item, file_transfer)

    @mcp.tool()
    @structured_tool
    async def flow_create_upload_link(
        project_id: str | None = None,
        label: str = "Reference or start-frame image",
    ) -> dict[str, Any]:
        """Mint a single-use HTTPS `/files/ul/{token}` endpoint (20 MiB max, 30m TTL) for large keyframes.

        Supports BOTH:
        1. Automated VM binary PUT/POST (no human needed, zero base64 LLM token overhead):
           `PUT <upload_url>?filename=frame.png` with raw image bytes -> immediately returns JSON `{"status": "completed", "media_id": "..."}`.
        2. Browser picker UI (if opened via GET in a browser), followed by `flow_check_upload(upload_id)`.
        """
        if file_transfer is None:
            raise ValidationError(
                "Signed HTTP upload links require HTTP transport with GFLOW_MCP_PUBLIC_URL or "
                "GFLOW_MCP_OAUTH_BASE_URL configured. Use flow_upload_image(image_base64=...) or file_path."
            )
        client = await manager.get()
        pid = await client.resolve_project_id(project_id)
        return file_transfer.create_upload_link(pid, label=label)

    @mcp.tool()
    @structured_tool
    async def flow_check_upload(upload_id: str) -> dict[str, Any]:
        """Check status of a single-use `upload_id` from `flow_create_upload_link` and retrieve its `media_id`."""
        if file_transfer is None:
            raise ValidationError("HTTP file transfer is not enabled on this transport.")
        st = file_transfer.jti_store.status(upload_id.strip())
        if st.get("status") == "unknown_or_expired":
            raise NotFoundError(f"Upload ID {upload_id!r} was not found or has expired.")
        return st

    @mcp.tool()
    @structured_tool
    async def flow_save_video_frame(
        video_media_id: str,
        timestamp_seconds: float = 0.0,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Extract a still frame from an existing Flow video clip at `timestamp_seconds` and return its reusable image `media_id`."""
        if timestamp_seconds < 0:
            raise ValidationError("timestamp_seconds must be >= 0.")
        client = await manager.get()
        item = await client.save_video_frame(
            project_id,
            video_media_id,
            timestamp_seconds=timestamp_seconds,
        )
        return _serialize_media(item, file_transfer)

    @mcp.tool()
    @structured_tool
    async def flow_get_media(
        media_id: str,
        include_checksum: bool = False,
        output_path: str | None = None,
        verify_dimensions: bool = False,
    ) -> dict[str, Any]:
        """Fetch full metadata + 24h signed `download_link` for a `media_id`.

        Set `include_checksum=True` to also return exact `file_size_bytes` and `sha256` from the server.
        Metadata `width`/`height` may be inferred from the aspect-ratio code
        (`dimensions_source='inferred_from_aspect'`). Set `verify_dimensions=True` (implied by
        `include_checksum=True`) to download the MP4 and measure the real frame size from its `tkhd` box:
        returns `measured_width`, `measured_height`, `dimensions_source='measured_mp4'` and
        `dimension_mismatch=True` when the measured size differs from the metadata.
        """
        client = await manager.get()
        item = await client.get_media(media_id)
        data = _serialize_media(item, file_transfer)
        data.setdefault("dimensions_source", getattr(item, "dimensions_source", None))
        raw_bytes: bytes | None = None
        if (include_checksum or verify_dimensions) and item.download_url:
            raw_bytes, _ = await client._rpc.download_bytes(item.download_url)
        if include_checksum and raw_bytes is not None:
            data["file_size_bytes"] = len(raw_bytes)
            data["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
        if raw_bytes is not None:
            measured = measure_mp4_dimensions(raw_bytes)
            if measured is not None:
                m_w, m_h = measured
                data["measured_width"] = m_w
                data["measured_height"] = m_h
                data["dimensions_source"] = "measured_mp4"
                data["width"] = m_w
                data["height"] = m_h
                if item.width is not None and item.height is not None:
                    mismatch = (item.width, item.height) != (m_w, m_h)
                    data["dimension_mismatch"] = mismatch
                    if mismatch:
                        data["metadata_width"] = item.width
                        data["metadata_height"] = item.height
                        data["metadata_dimensions_source"] = getattr(item, "dimensions_source", None)
            elif verify_dimensions:
                data["dimension_probe_failed"] = True
        if output_path is not None:
            saved = await client.download_media(item, output_path=output_path)
            data["saved_path"] = str(saved)
        return data

    @mcp.tool()
    @structured_tool
    async def flow_poll_media(
        media_ids: list[str] | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        wait: bool = False,
        timeout_seconds: float = DEFAULT_SOFT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Query generation or export job progress by `job_id`, `idempotency_key`, or `media_ids`."""
        resolved_job_id: str | None = None
        record: dict[str, Any] | None = None
        if job_id or idempotency_key:
            record = jobs.lookup(job_id=job_id, idempotency_key=idempotency_key)
            resolved_job_id = str(record["job_id"])
            if record.get("kind") == "scene_export":
                return record
            if not media_ids:
                media_ids = list(record.get("media_ids") or [])

        if not media_ids:
            raise ValidationError(
                "Provide `job_id`, `idempotency_key`, or a non-empty `media_ids` list."
            )

        client = await manager.get()
        status, items = await _poll_soft(
            client,
            media_ids,
            wait=wait,
            wait_timeout_seconds=timeout_seconds,
        )
        if resolved_job_id:
            await jobs.update_job(resolved_job_id, {"status": status})
        return {
            "job_id": resolved_job_id,
            "status": status,
            "media_ids": list(media_ids),
            "items": [_serialize_media(it, file_transfer) for it in items],
        }

    # ── 4. Nano Banana Image & Veo / Omni Video Generation Tools ─────────────

    @mcp.tool()
    @structured_tool
    async def flow_generate_image(
        prompt: str,
        project_id: str | None = None,
        model: str = "pro",
        aspect_ratio: str = "16:9",
        count: int = 1,
        reference_media_ids: list[str] | None = None,
        base_image_media_id: str | None = None,
        seed: int | None = None,
        idempotency_key: str | None = None,
        wait: bool = True,
        wait_timeout_seconds: float = DEFAULT_SOFT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Generate Nano Banana images (`pro`=GEM_PIX_2, `2`/`2.1`=BELUGA, `lite`=HARBOR_SEAL).

        Returns `job_id`, `status`, `media_ids`, and `items` (each with `media_id` & 24h `download_link`).
        Supports `idempotency_key` so retries never create duplicate jobs.
        """
        if not prompt.strip():
            raise ValidationError("prompt must not be empty.")
        if not 1 <= count <= 4:
            raise ValidationError("count must be between 1 and 4.")
        resolve_image_aspect(aspect_ratio)
        resolve_image_model_key(model)

        return await _run_media_generation_job(
            op="generate_image",
            params={
                "prompt": prompt,
                "project_id": project_id,
                "model": model,
                "aspect_ratio": aspect_ratio,
                "count": count,
                "reference_media_ids": list(reference_media_ids) if reference_media_ids else None,
                "base_image_media_id": base_image_media_id,
                "seed": seed,
            },
            idempotency_key=idempotency_key,
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
            submit_coro_fn=lambda client: client.generate_image(
                project_id,
                prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                count=count,
                references=list(reference_media_ids) if reference_media_ids else None,
                base_image=base_image_media_id,
                seed=seed,
                wait=False,
            ),
        )

    @mcp.tool()
    @structured_tool
    async def flow_generate_video(
        prompt: str,
        project_id: str | None = None,
        model: str = "omni",
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        duration_seconds: int = 4,
        count: int = 1,
        start_frame_media_id: str | None = None,
        end_frame_media_id: str | None = None,
        reference_media_ids: list[str] | None = None,
        scene_id: str | None = None,
        seed: int | None = None,
        idempotency_key: str | None = None,
        wait: bool = True,
        wait_timeout_seconds: float = DEFAULT_SOFT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Generate video clips in Google Flow using Omni 1.2 Flash (`omni`, default, 3..15s) or Veo 3.1 (`fast`, `quality`, `lite`).

        Modes automatically selected from supplied inputs:
        - Text-to-Video: only `prompt`
        - Start-Frame Image-to-Video: `start_frame_media_id`
        - Start + End Frame Interpolation: `start_frame_media_id` + `end_frame_media_id`
        - Multi-Reference R2V: `reference_media_ids` (1..3 media IDs)

        Always persists and returns `job_id` before polling. If rendering exceeds `wait_timeout_seconds`
        (default 25s, max 35s), returns `status='running'` + `job_id` cleanly so client connections
        never time out; call `flow_poll_media(job_id=...)` to fetch the finished `download_link`.
        Pass `idempotency_key` to make retries 100% deduplicated (zero duplicate credit charges).
        """
        if not prompt.strip():
            raise ValidationError("prompt must not be empty.")
        if not 1 <= count <= 4:
            raise ValidationError("count must be between 1 and 4.")
        if end_frame_media_id and not start_frame_media_id:
            raise ValidationError("end_frame_media_id requires start_frame_media_id.")
        if reference_media_ids and len(reference_media_ids) > 3:
            raise ValidationError("reference_media_ids supports at most 3 reference images.")
        _, is_portrait = resolve_video_aspect(aspect_ratio)
        resolve_video_resolution(resolution)
        if start_frame_media_id and end_frame_media_id:
            v_mode = "START_END_FRAMES"
        elif start_frame_media_id:
            v_mode = "START_FRAME"
        elif reference_media_ids:
            v_mode = "REFERENCES"
        else:
            v_mode = "TEXT"
        resolve_video_model_key(
            model,
            mode=v_mode,
            is_portrait=is_portrait,
            duration_seconds=duration_seconds,
        )

        return await _run_media_generation_job(
            op="generate_video",
            params={
                "prompt": prompt,
                "project_id": project_id,
                "model": model,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "duration_seconds": duration_seconds,
                "count": count,
                "start_frame_media_id": start_frame_media_id,
                "end_frame_media_id": end_frame_media_id,
                "reference_media_ids": list(reference_media_ids) if reference_media_ids else None,
                "scene_id": scene_id,
                "seed": seed,
            },
            idempotency_key=idempotency_key,
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
            submit_coro_fn=lambda client: client.generate_video(
                project_id,
                prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                duration_seconds=duration_seconds,
                count=count,
                start_frame=start_frame_media_id,
                end_frame=end_frame_media_id,
                references=list(reference_media_ids) if reference_media_ids else None,
                scene_id=scene_id,
                seed=seed,
                wait=False,
            ),
        )

    @mcp.tool()
    @structured_tool
    async def flow_extend_video(
        video_media_id: str,
        prompt: str,
        project_id: str | None = None,
        model: str = "omni",
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        reference_media_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        wait: bool = True,
        wait_timeout_seconds: float = DEFAULT_SOFT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Extend an existing Flow video clip (`video_media_id`) with `job_id` & `idempotency_key` safety."""
        if not prompt.strip():
            raise ValidationError("prompt must not be empty.")
        if not video_media_id.strip():
            raise ValidationError("video_media_id must not be empty.")
        if reference_media_ids and len(reference_media_ids) > 3:
            raise ValidationError("reference_media_ids supports at most 3 reference images.")
        _, is_portrait = resolve_video_aspect(aspect_ratio)
        resolve_video_resolution(resolution)
        resolve_video_model_key(model, mode="EXTEND_VIDEO", is_portrait=is_portrait)

        return await _run_media_generation_job(
            op="extend_video",
            params={
                "video_media_id": video_media_id,
                "prompt": prompt,
                "project_id": project_id,
                "model": model,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "reference_media_ids": list(reference_media_ids) if reference_media_ids else None,
            },
            idempotency_key=idempotency_key,
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
            submit_coro_fn=lambda client: client.extend_video(
                project_id,
                video_media_id,
                prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                references=list(reference_media_ids) if reference_media_ids else None,
                wait=False,
            ),
        )

    @mcp.tool()
    @structured_tool
    async def flow_upsample_video(
        video_media_id: str,
        target: str = "1080p",
        aspect_ratio: str | None = None,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        wait: bool = True,
        wait_timeout_seconds: float = DEFAULT_SOFT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Upsample an existing 720p Flow video clip (`video_media_id`) to `1080p` or `4k` (auto-preserves 9:16 / 16:9 aspect ratio when `aspect_ratio` is omitted)."""
        if not video_media_id.strip():
            raise ValidationError("video_media_id must not be empty.")
        is_portrait = False
        if aspect_ratio is not None and aspect_ratio.strip():
            _, is_portrait = resolve_video_aspect(aspect_ratio)
        resolve_video_model_key(target, mode="UPSAMPLE_VIDEO", is_portrait=is_portrait)
        resolve_video_resolution(target)

        return await _run_media_generation_job(
            op="upsample_video",
            params={
                "video_media_id": video_media_id,
                "target": target,
                "aspect_ratio": aspect_ratio,
                "project_id": project_id,
            },
            idempotency_key=idempotency_key,
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
            submit_coro_fn=lambda client: client.upsample_video(
                project_id,
                video_media_id,
                target=target,
                aspect_ratio=aspect_ratio,
                wait=False,
            ),
        )

    # ── 5. Scene Builder Storyboarding & MP4 Export Tools ────────────────────

    @mcp.tool()
    @structured_tool
    async def flow_scene_list(project_id: str | None = None) -> list[dict[str, Any]]:
        """List all Scene Builder multi-clip timelines in a Flow project."""
        client = await manager.get()
        scenes = await client.list_scenes(project_id)
        return [s.to_dict() for s in scenes]

    @mcp.tool()
    @structured_tool
    async def flow_scene_get(scene_id: str, project_id: str | None = None) -> dict[str, Any]:
        """Inspect ordered clips and trim timestamps inside a Scene Builder timeline."""
        client = await manager.get()
        scene = await client.get_scene(project_id, scene_id)
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_create(
        title: str = "Untitled scene",
        aspect_ratio: str = "16:9",
        clip_ids: list[str] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Scene Builder timeline, optionally pre-populated with `workflow_id` or `media_id` clips."""
        client = await manager.get()
        scene = await client.create_scene(
            project_id,
            title=title,
            aspect_ratio=aspect_ratio,
            clip_ids=clip_ids,
        )
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_rename(
        scene_id: str,
        new_title: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename a Scene Builder timeline."""
        if not new_title.strip():
            raise ValidationError("new_title must not be empty.")
        client = await manager.get()
        scene = await client.rename_scene(project_id, scene_id, new_title)
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_add_clips(
        scene_id: str,
        clip_ids: list[str],
        position: int | None = None,
        start_time_seconds: float | None = None,
        end_time_seconds: float | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Append or insert video clips (`media_id` or `workflow_id`) into a Scene Builder timeline."""
        if not clip_ids:
            raise ValidationError("clip_ids must not be empty.")
        client = await manager.get()
        scene = await client.add_scene_clips(
            project_id,
            scene_id,
            clip_ids,
            position=position,
            start_time_seconds=start_time_seconds,
            end_time_seconds=end_time_seconds,
        )
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_reorder_clip(
        scene_id: str,
        from_index: int,
        to_index: int,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a clip from 0-based `from_index` to `to_index` in a Scene Builder timeline."""
        client = await manager.get()
        scene = await client.reorder_scene_clip(project_id, scene_id, from_index, to_index)
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_trim_clip(
        scene_id: str,
        clip_index: int,
        start_time_seconds: float | None = None,
        end_time_seconds: float | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Adjust in/out trim timestamps (`start_time_seconds` / `end_time_seconds`) of clip `clip_index`."""
        client = await manager.get()
        scene = await client.trim_scene_clip(
            project_id,
            scene_id,
            clip_index,
            start_time_seconds=start_time_seconds,
            end_time_seconds=end_time_seconds,
        )
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_remove_clip(
        scene_id: str,
        clip_index: int,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Remove the clip at 0-based `clip_index` from a Scene Builder timeline."""
        client = await manager.get()
        scene = await client.remove_scene_clip(project_id, scene_id, clip_index)
        return scene.to_dict()

    @mcp.tool()
    @structured_tool
    async def flow_scene_export_mp4(
        scene_id: str,
        output_path: str | None = None,
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Render and stitch a Scene Builder timeline into a single continuous MP4 video.

        Saves the stitched MP4 on server disk (`~/.gflow/exports/`, 7-day retention) and returns
        a 24-hour signed HTTPS `download_link` + exact `file_size_bytes` and `sha256` checksum so
        remote VM callers can pull and verify byte-exactness directly without human relay.
        """
        from ._fileroutes import compute_file_checksum, get_exports_dir, prune_expired_exports

        prune_expired_exports()
        client = await manager.get()

        async def _do_export(jid: str) -> dict[str, Any]:
            ts = int(time.time())
            export_fname = f"gflow_scene_{scene_id.strip()[:8]}_{ts}_{jid[-6:]}.mp4"
            managed_copy = get_exports_dir() / export_fname
            upstream_url, saved = await client.export_scene_mp4(
                project_id,
                scene_id,
                output_path=managed_copy,
            )
            assert saved is not None
            if output_path is not None:
                custom_dest = Path(output_path).expanduser()
                if custom_dest.is_dir():
                    custom_dest = custom_dest / export_fname
                custom_dest.parent.mkdir(parents=True, exist_ok=True)
                if custom_dest.resolve() != saved.resolve():
                    custom_dest.write_bytes(saved.read_bytes())

            size_bytes, sha_hex = compute_file_checksum(saved)
            measured = measure_mp4_dimensions(saved.read_bytes())
            return {
                "kind": "scene_export",
                "status": "completed",
                "scene_id": scene_id.strip(),
                "export_filename": export_fname,
                "saved_path": str(saved),
                "upstream_mp4_url": upstream_url,
                "file_size_bytes": size_bytes,
                "sha256": sha_hex,
                "measured_width": measured[0] if measured else None,
                "measured_height": measured[1] if measured else None,
            }

        record, replayed = await jobs.submit_or_reuse(
            op="scene_export",
            idempotency_key=idempotency_key,
            submit_fn=_do_export,
            params_fingerprint=_params_fingerprint(
                {"scene_id": scene_id.strip(), "project_id": project_id, "output_path": output_path}
            ),
        )
        result = {
            **record,
            "idempotent_replay": replayed,
        }
        if file_transfer is not None and record.get("export_filename"):
            result.update(file_transfer.download_link_for_export(str(record["export_filename"])))
        return result

    return mcp
