"""Out-of-band signed HTTP media download & VM/browser image upload routes for FastMCP."""

from __future__ import annotations

import hashlib
import html
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from ..client import FlowClient
from ..exceptions import GFlowError, NotFoundError
from ..paths import get_home_dir
from ._filelink import FileLinkError, FileTransferConfig

if TYPE_CHECKING:
    from fastmcp import FastMCP

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB exact Google Flow upstream image cap (`20,971,520` bytes)
ALLOWED_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
EXPORT_RETENTION_SECONDS = 7 * 86400  # 7 days local export cache retention

_MAX_CONCURRENT_UPLOADS = 4
_MAX_CONCURRENT_DOWNLOADS = 4
_inflight_uploads = 0
_inflight_downloads = 0

_UPLOAD_REJECTED_MSG = "This upload link is invalid, already used, or expired."

_HTML_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src blob: data:; form-action 'none'; base-uri 'none'"
    ),
}

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Max-Age": "600",
}
_CORS_ORIGIN = {"Access-Control-Allow-Origin": "*"}

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_filename(raw: str | None, fallback: str = "upload.png") -> str:
    if not raw:
        return fallback
    name = Path(raw).name.strip()
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned[:120] if cleaned else fallback


def get_exports_dir() -> Path:
    d = get_home_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_expired_exports(max_age_seconds: int = EXPORT_RETENTION_SECONDS) -> int:
    """Remove cached Scene MP4 exports older than ``max_age_seconds`` (default 7 days)."""
    exports_dir = get_exports_dir()
    now = time.time()
    removed = 0
    for f in exports_dir.glob("*.mp4"):
        try:
            if now - f.stat().st_mtime > max_age_seconds:
                f.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


def compute_file_checksum(path: Path) -> tuple[int, str]:
    """Return ``(size_bytes, sha256_hex)`` for a local file."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fp:
        while chunk := fp.read(256 * 1024):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def register_file_routes(
    mcp: FastMCP,
    config: FileTransferConfig,
    get_client: Callable[[], Awaitable[FlowClient]],
) -> None:
    """Mount ``/files/dl/{token}`` and ``/files/ul/{token}`` custom routes on ``mcp``."""

    @mcp.custom_route("/files/dl/{token}", methods=["GET"])
    async def download_route(request: Request) -> Response:
        global _inflight_downloads
        token = request.path_params["token"]
        try:
            payload = config.signer.verify(token, op="dl")
        except FileLinkError:
            return PlainTextResponse(
                "This download link is invalid or has expired.",
                status_code=403,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )

        if _inflight_downloads >= _MAX_CONCURRENT_DOWNLOADS:
            return PlainTextResponse(
                "Too many concurrent downloads (limit=4); retry shortly.",
                status_code=429,
            )

        _inflight_downloads += 1
        try:
            disposition_mode = (
                "attachment" if request.query_params.get("download") == "1" else "inline"
            )
            kind = payload.get("kind", "media")

            if kind == "export":
                fname = _safe_filename(str(payload.get("filename") or ""), fallback="")
                if not fname:
                    return PlainTextResponse("Export file not found.", status_code=404)
                export_path = (get_exports_dir() / fname).resolve()
                if not export_path.is_file() or export_path.parent != get_exports_dir().resolve():
                    return PlainTextResponse("Export file not found or expired.", status_code=404)
                size_bytes, sha_hex = compute_file_checksum(export_path)
                return FileResponse(
                    export_path,
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": f'{disposition_mode}; filename="{fname}"',
                        "Cache-Control": "private, max-age=300",
                        "Referrer-Policy": "no-referrer",
                        "X-GFlow-Bytes": str(size_bytes),
                        "X-GFlow-SHA256": sha_hex,
                    },
                )

            media_id = str(payload.get("media_id") or "").strip()
            if not media_id:
                return PlainTextResponse("Missing media_id in token.", status_code=400)

            client = await get_client()
            item = await client.get_media(media_id)
            if not item.download_url:
                return PlainTextResponse(
                    f"Media {media_id} is not ready yet (status={item.status}).",
                    status_code=409,
                )

            data, upstream_ctype = await client._rpc.download_bytes(item.download_url)
            ctype = (
                upstream_ctype
                or item.mime_type
                or ("video/mp4" if item.media_type == "video" else "image/jpeg")
            )
            ext = ".mp4" if "video" in ctype else (".png" if "png" in ctype else ".jpg")
            fname = f"gflow_{item.media_type}_{item.id[:8]}{ext}"
            sha_hex = hashlib.sha256(data).hexdigest()
            return Response(
                content=data,
                media_type=ctype,
                headers={
                    "Content-Disposition": f'{disposition_mode}; filename="{fname}"',
                    "Content-Length": str(len(data)),
                    "Cache-Control": "private, max-age=300",
                    "Referrer-Policy": "no-referrer",
                    "X-GFlow-Bytes": str(len(data)),
                    "X-GFlow-SHA256": sha_hex,
                },
            )
        except NotFoundError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        except GFlowError as exc:
            return PlainTextResponse(f"Upstream Google Flow error: {exc}", status_code=502)
        finally:
            _inflight_downloads -= 1

    @mcp.custom_route("/files/ul/{token}", methods=["GET"])
    async def upload_page_route(request: Request) -> Response:
        token = request.path_params["token"]
        try:
            payload = config.signer.verify(token, op="ul")
        except FileLinkError:
            return HTMLResponse(
                "<!doctype html><html><body style='font-family:system-ui;padding:2rem'>"
                "<h2>Upload link expired or invalid</h2>"
                "<p>Ask your AI assistant to generate a fresh upload link.</p>"
                "</body></html>",
                status_code=403,
                headers=_HTML_SECURITY_HEADERS,
            )

        jti = str(payload.get("jti") or "")
        st = config.jti_store.status(jti)
        if st.get("status") == "completed":
            mid = html.escape(str(st.get("media_id") or ""))
            return HTMLResponse(
                "<!doctype html><html><body style='font-family:system-ui;padding:2rem'>"
                "<h2>Image already uploaded</h2>"
                f"<p>Media ID: <code>{mid}</code></p>"
                "<p>Return to your assistant — it can now use this image in Google Flow.</p>"
                "</body></html>",
                status_code=200,
                headers=_HTML_SECURITY_HEADERS,
            )

        label = html.escape(str(payload.get("label") or "Reference image"))
        pid = html.escape(str(payload.get("project_id") or ""))
        page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload to Google Flow</title>
</head>
<body style="font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;margin:0;display:flex;justify-content:center;padding-top:8vh">
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px 32px;max-width:480px;width:92%;box-shadow:0 8px 24px rgba(15,23,42,0.06)">
    <h2 style="margin:0 0 6px;color:#0f172a">Upload to Google Flow</h2>
    <p style="margin:0 0 18px;color:#475569;font-size:0.95rem">{label} &middot; Project <code>{pid[:8]}...</code></p>
    <input id="fileInput" type="file" accept="image/png,image/jpeg,image/webp" style="display:block;width:100%;margin-bottom:16px;font-size:0.95rem">
    <button id="btn" type="button" style="width:100%;padding:11px 16px;font-size:1rem;font-weight:600;background:#0f172a;color:#fff;border:none;border-radius:8px;cursor:pointer">Upload Image</button>
    <div id="status" style="margin-top:16px;font-size:0.92rem;color:#334155;word-break:break-all"></div>
  </div>
<script>
const btn = document.getElementById('btn');
const inp = document.getElementById('fileInput');
const st = document.getElementById('status');
btn.addEventListener('click', async () => {{
  const f = inp.files && inp.files[0];
  if (!f) {{ st.textContent = 'Please choose an image file first.'; return; }}
  btn.disabled = true;
  st.textContent = 'Uploading to Google Flow (running Enterprise reCAPTCHA + RPC)...';
  try {{
    const u = new URL(window.location.href);
    u.searchParams.set('filename', f.name);
    const resp = await fetch(u.toString(), {{
      method: 'POST',
      headers: {{ 'Content-Type': f.type || 'image/png' }},
      body: f
    }});
    const text = await resp.text();
    if (!resp.ok) {{
      st.style.color = '#dc2626';
      st.textContent = 'Upload failed (' + resp.status + '): ' + text;
      btn.disabled = false;
      return;
    }}
    const data = JSON.parse(text);
    st.style.color = '#15803d';
    st.innerHTML = '<b>Uploaded!</b> Media ID: <code>' + data.media_id + '</code><br>You can close this tab and tell your assistant to continue.';
  }} catch (err) {{
    st.style.color = '#dc2626';
    st.textContent = 'Network error: ' + err;
    btn.disabled = false;
  }}
}});
</script>
</body>
</html>"""
        return HTMLResponse(page, status_code=200, headers=_HTML_SECURITY_HEADERS)

    @mcp.custom_route("/files/ul/{token}", methods=["OPTIONS"])
    async def upload_options_route(_request: Request) -> Response:
        return Response(status_code=204, headers=_CORS_HEADERS)

    @mcp.custom_route("/files/ul/{token}", methods=["POST", "PUT"])
    async def upload_post_route(request: Request) -> Response:
        global _inflight_uploads
        token = request.path_params["token"]
        try:
            payload = config.signer.verify(token, op="ul")
        except FileLinkError:
            return PlainTextResponse(_UPLOAD_REJECTED_MSG, status_code=403, headers=_CORS_ORIGIN)

        jti = payload.get("jti")
        if not isinstance(jti, str) or not jti:
            return PlainTextResponse(_UPLOAD_REJECTED_MSG, status_code=403, headers=_CORS_ORIGIN)

        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_UPLOAD_BYTES:
                    return PlainTextResponse(
                        "Image exceeds the 20 MiB upload limit.",
                        status_code=413,
                        headers=_CORS_ORIGIN,
                    )
            except ValueError:
                pass

        if not config.jti_store.try_begin(jti):
            return PlainTextResponse(_UPLOAD_REJECTED_MSG, status_code=403, headers=_CORS_ORIGIN)

        committed = False
        try:
            if _inflight_uploads >= _MAX_CONCURRENT_UPLOADS:
                return PlainTextResponse(
                    "Too many concurrent uploads (limit=4); retry shortly.",
                    status_code=429,
                    headers=_CORS_ORIGIN,
                )
            _inflight_uploads += 1
            try:
                chunks: list[bytes] = []
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        return PlainTextResponse(
                            "Image exceeds the 20 MiB upload limit.",
                            status_code=413,
                            headers=_CORS_ORIGIN,
                        )
                    chunks.append(chunk)
                raw_bytes = b"".join(chunks)
                if not raw_bytes:
                    return PlainTextResponse(
                        "Empty request body.",
                        status_code=400,
                        headers=_CORS_ORIGIN,
                    )

                fname = _safe_filename(request.query_params.get("filename"), fallback="upload.png")
                detected_mime: str | None = None
                if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    detected_mime = "image/png"
                elif raw_bytes.startswith(b"\xff\xd8\xff"):
                    detected_mime = "image/jpeg"
                elif len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
                    detected_mime = "image/webp"

                hdr_ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
                if hdr_ctype in ("image/jpg",):
                    hdr_ctype = "image/jpeg"
                if detected_mime is None or (
                    hdr_ctype
                    and hdr_ctype not in ("application/octet-stream", "")
                    and hdr_ctype not in ALLOWED_IMAGE_MIMES
                ):
                    return PlainTextResponse(
                        "Unsupported image format or MIME type. Supported: image/png, image/jpeg, image/webp.",
                        status_code=415,
                        headers=_CORS_ORIGIN,
                    )
                mime = detected_mime

                project_id = str(payload.get("project_id") or "")
                client = await get_client()
                item = await client.upload_image(
                    project_id,
                    raw_bytes,
                    file_name=fname,
                    mime_type=mime,
                )
                dl_info = config.download_link_for_media(item.id)
                result = {
                    "media_id": item.id,
                    "project_id": item.project_id or project_id,
                    "media_type": item.media_type,
                    "mime_type": mime,
                    "width": item.width,
                    "height": item.height,
                    "file_name": fname,
                    "file_size_bytes": len(raw_bytes),
                    **dl_info,
                }
                config.jti_store.commit(jti, float(payload["exp"]), result)
                committed = True
                return JSONResponse({"status": "completed", **result}, headers=_CORS_ORIGIN)
            except GFlowError as exc:
                return PlainTextResponse(
                    f"Google Flow upload error ({exc.error_code}): {exc}",
                    status_code=502,
                    headers=_CORS_ORIGIN,
                )
            finally:
                _inflight_uploads -= 1
        finally:
            if not committed:
                config.jti_store.rollback(jti)
