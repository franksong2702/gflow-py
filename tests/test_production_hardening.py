"""Production-hardening tests covering remote MCP connector guarantees (`P0-1` .. `P2-6`)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from gflow._rpc import FlowHttpRpc, _classify_rpc_error
from gflow.exceptions import (
    ERR_AUTH_EXPIRED,
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
)
from gflow.mcp._filelink import DEFAULT_DOWNLOAD_TTL_SECONDS, FileLinkSigner, FileTransferConfig
from gflow.mcp._fileroutes import get_exports_dir, prune_expired_exports
from gflow.mcp._jobs import JobStore
from gflow.mcp.server import create_server
from gflow.types import AccountCredits, MediaItem, Project, Scene, UserAccount

VALID_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
VALID_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
VALID_WEBP = b"RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00\x00\x00\x00\x00"
FAKE_VIDEO_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"V" * 4096)
FAKE_SCENE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"S" * 8192)


class HardenedMockClient:
    """Deterministic stateful mock FlowClient for production hardening tests."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int]] = []
        self.video_submissions: list[dict[str, Any]] = []
        self.scene_exports: int = 0
        self.credits_balance: int = 1000
        self.poll_ready_after: int = 0  # number of polls before download_url appears
        self._poll_counts: dict[str, int] = {}
        self.next_error: Exception | None = None
        self._rpc = self

    async def __aenter__(self) -> HardenedMockClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def download_bytes(self, url: str) -> tuple[bytes, str]:
        return FAKE_VIDEO_MP4, "video/mp4"

    def _maybe_raise(self) -> None:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err

    async def whoami(self) -> UserAccount:
        self._maybe_raise()
        return UserAccount(user_id="u1", email="agent@example.com", display_name="Remote Agent")

    async def get_credits(self) -> AccountCredits:
        self._maybe_raise()
        return AccountCredits(total_credits=self.credits_balance, subscription_credits=self.credits_balance)

    async def resolve_project_id(self, pid: str | None = None) -> str:
        return pid or "proj-1111"

    async def get_project(self, pid: str) -> Project:
        self._maybe_raise()
        if pid == "missing-proj":
            raise NotFoundError("Project missing-proj not found.")
        return Project(id=pid, title="Hardened Project")

    async def upload_image(
        self,
        project_id: str | None,
        source: str | Path | bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        is_hidden: bool = False,
    ) -> MediaItem:
        self._maybe_raise()
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        self.uploads.append((file_name or "frame.png", mime_type or "image/png", len(raw)))
        return MediaItem(
            id=f"img-{len(self.uploads):03d}",
            project_id=project_id or "proj-1111",
            media_type="image",
            status="completed",
            mime_type=mime_type or "image/png",
            image_url="https://storage.googleapis.com/fake/img.png",
        )

    async def generate_video(self, project_id: str | None, prompt: str, **kwargs: Any) -> list[MediaItem]:
        self._maybe_raise()
        await asyncio.sleep(0.05)  # allow concurrent idempotency race tests
        self.video_submissions.append({"prompt": prompt, **kwargs})
        self.credits_balance -= 20
        vid_id = f"vid-{len(self.video_submissions):03d}"
        return [
            MediaItem(
                id=vid_id,
                project_id=project_id or "proj-1111",
                media_type="pending",
                status="running",
                prompt=prompt,
            )
        ]

    async def get_media(self, media_id: str) -> MediaItem:
        self._maybe_raise()
        if media_id == "missing-media":
            raise NotFoundError(f"Media {media_id} not found.")
        return MediaItem(
            id=media_id,
            project_id="proj-1111",
            media_type="video",
            status="completed",
            mime_type="video/mp4",
            video_url="https://storage.googleapis.com/fake/vid.mp4",
        )

    async def poll_media(self, media_ids: list[str], **kwargs: Any) -> list[MediaItem]:
        self._maybe_raise()
        out: list[MediaItem] = []
        for mid in media_ids:
            cnt = self._poll_counts.get(mid, 0) + 1
            self._poll_counts[mid] = cnt
            if cnt <= self.poll_ready_after:
                out.append(MediaItem(id=mid, project_id="proj-1111", media_type="pending", status="running"))
            else:
                out.append(
                    MediaItem(
                        id=mid,
                        project_id="proj-1111",
                        media_type="video",
                        status="completed",
                        mime_type="video/mp4",
                        video_url="https://storage.googleapis.com/fake/vid.mp4",
                    )
                )
        return out

    async def export_scene_mp4(
        self,
        project_id: str | None,
        scene_id: str,
        *,
        output_path: str | Path | None = None,
        **kwargs: Any,
    ) -> tuple[str, Path | None]:
        self._maybe_raise()
        self.scene_exports += 1
        assert output_path is not None
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_SCENE_MP4)
        return "https://storage.googleapis.com/fake/scene_export.mp4", out


def _extract_json(result: Any) -> dict[str, Any]:
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        return result.structured_content
    texts = getattr(result, "content", result)
    raw = texts[0].text
    return json.loads(raw)


# ── P0-1 & P0-2: Direct Base64 Upload + Byte-Exact Video & Scene Export Download ──


async def test_p0_1_and_p0_2_remote_upload_and_byte_exact_download(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    ft = FileTransferConfig(
        signer=FileLinkSigner.from_persistent_store(tmp_path / "gflow_home" / "secret.key"),
        base_url="https://flow-host.example.ts.net:8443",
    )
    assert ft.download_ttl_seconds == DEFAULT_DOWNLOAD_TTL_SECONDS == 86400

    server = create_server(
        client_factory=lambda: mock,  # type: ignore[arg-type]
        file_transfer=ft,
        job_store=JobStore(tmp_path / "gflow_home" / "jobs.json"),
    )

    # 1. P0-1: Direct base64 upload (PNG, JPEG, data-URL WebP) without human browser relay
    b64_png = base64.b64encode(VALID_PNG).decode("ascii")
    up_res = _extract_json(
        await server.call_tool(
            "flow_upload_image",
            {"image_base64": b64_png, "file_name": "shot01_keyframe.png"},
        )
    )
    assert up_res["media_id"] == "img-001"
    assert up_res["download_ttl_seconds"] == 86400
    assert "download_expires_at" in up_res

    data_url_webp = "data:image/webp;base64," + base64.b64encode(VALID_WEBP).decode("ascii")
    up_webp = _extract_json(
        await server.call_tool("flow_upload_image", {"image_base64": data_url_webp, "file_name": "ref"})
    )
    assert up_webp["media_id"] == "img-002"
    assert mock.uploads[-1][1] == "image/webp"

    # 2. P0-1 -> Generate video using uploaded `media_id` as start_frame_media_id
    gen_res = _extract_json(
        await server.call_tool(
            "flow_generate_video",
            {
                "prompt": "Slow dolly in on steamed rice cake",
                "start_frame_media_id": up_res["media_id"],
                "model": "fast",
            },
        )
    )
    assert gen_res["status"] == "completed"
    assert mock.video_submissions[0]["start_frame"] == "img-001"
    vid_item = gen_res["items"][0]
    vid_dl_path = "/" + vid_item["download_link"].split(":8443/", 1)[1]

    # Verify flow_get_media(include_checksum=True) matches exact bytes & SHA-256
    meta_res = _extract_json(
        await server.call_tool("flow_get_media", {"media_id": vid_item["media_id"], "include_checksum": True})
    )
    expected_vid_sha = hashlib.sha256(FAKE_VIDEO_MP4).hexdigest()
    assert meta_res["file_size_bytes"] == len(FAKE_VIDEO_MP4)
    assert meta_res["sha256"] == expected_vid_sha

    # 3. P0-2: Scene Builder MP4 export + remote HTTPS download byte-exact verification
    exp_res = _extract_json(
        await server.call_tool(
            "flow_scene_export_mp4",
            {"scene_id": "scene-demo-01", "idempotency_key": "export-v1"},
        )
    )
    expected_exp_sha = hashlib.sha256(FAKE_SCENE_MP4).hexdigest()
    assert exp_res["file_size_bytes"] == len(FAKE_SCENE_MP4)
    assert exp_res["sha256"] == expected_exp_sha
    assert exp_res["download_ttl_seconds"] == 86400
    exp_dl_path = "/" + exp_res["download_link"].split(":8443/", 1)[1]

    asgi_app = server.http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="https://flow-host.example.ts.net:8443",
    ) as http:
        r_vid = await http.get(vid_dl_path)
        assert r_vid.status_code == 200
        assert len(r_vid.content) == len(FAKE_VIDEO_MP4)
        assert int(r_vid.headers["X-GFlow-Bytes"]) == len(FAKE_VIDEO_MP4)
        assert r_vid.headers["X-GFlow-SHA256"] == expected_vid_sha
        assert r_vid.content == FAKE_VIDEO_MP4

        r_exp = await http.get(exp_dl_path)
        assert r_exp.status_code == 200
        assert len(r_exp.content) == len(FAKE_SCENE_MP4)
        assert int(r_exp.headers["X-GFlow-Bytes"]) == len(FAKE_SCENE_MP4)
        assert r_exp.headers["X-GFlow-SHA256"] == expected_exp_sha
        assert r_exp.content == FAKE_SCENE_MP4


# ── P0-3: Automatic Google Session Renewal & Immediate `AUTH_EXPIRED` ─────────


async def test_p0_3_session_auto_renewal_and_early_auth_expired() -> None:
    calls = {"html": 0, "recover": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" or "flow.google.com" in str(request.url):
            calls["html"] += 1
            has_fresh = "fresh_psidts" in (request.headers.get("cookie") or "")
            if not has_fresh:
                return httpx.Response(
                    302,
                    headers={"Location": "https://accounts.google.com/ServiceLogin"},
                )
            wiz_html = (
                '<html><script>window.WIZ_global_data = '
                '{"SNlM0e":"xsrf_abc:123","FdrFJe":"987654","cfb2h":"boq_labs_2026"};'
                "</script></html>"
            )
            return httpx.Response(200, text=wiz_html)
        return httpx.Response(404)

    stale_cookies = httpx.Cookies()
    stale_cookies.set("SID", "sid_val", domain=".google.com")

    async def recover_ok() -> httpx.Cookies:
        calls["recover"] += 1
        healed = httpx.Cookies()
        healed.set("SID", "sid_val", domain=".google.com")
        healed.set("__Secure-1PSIDTS", "fresh_psidts", domain=".google.com")
        return healed

    rpc = FlowHttpRpc(stale_cookies, on_auth_expired=recover_ok)
    # Inject mock transport on both initial & replaced clients
    orig_build = rpc._build_http_client
    rpc._build_http_client = lambda c: httpx.AsyncClient(  # type: ignore[method-assign]
        cookies=c,
        transport=httpx.MockTransport(handler),
    )
    await rpc._client.aclose()
    rpc._client = rpc._build_http_client(stale_cookies)

    wiz = await rpc.bootstrap(force=True)
    assert wiz.xsrf_token == "xsrf_abc:123"
    assert calls["recover"] == 1
    await rpc.close()

    # Now test permanently invalidated credentials -> immediate AUTH_EXPIRED (<0.1s, no hang)
    async def recover_fail() -> httpx.Cookies:
        raise AuthError("Refresh cookies revoked.")

    rpc_dead = FlowHttpRpc(stale_cookies, on_auth_expired=recover_fail)
    await rpc_dead._client.aclose()
    rpc_dead._client = httpx.AsyncClient(cookies=stale_cookies, transport=httpx.MockTransport(handler))
    t0 = time.monotonic()
    with pytest.raises(AuthError) as exc_info:
        await rpc_dead.bootstrap(force=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert exc_info.value.error_code == ERR_AUTH_EXPIRED
    await rpc_dead.close()
    _ = orig_build


# ── P1-4: Soft-Timeout `job_id`, Mid-Flight Disconnect Shield, & Idempotency ─


async def test_p1_4_soft_timeout_disconnect_safety_and_idempotency(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    mock.poll_ready_after = 3  # first 3 polls stay "running", 4th completes
    jobs = JobStore(tmp_path / "gflow_home" / "jobs.json")
    server = create_server(client_factory=lambda: mock, job_store=jobs)  # type: ignore[arg-type]

    # 1. Soft-wait expires after 1s -> returns job_id + status="running" before client timeout
    res1 = _extract_json(
        await server.call_tool(
            "flow_generate_video",
            {
                "prompt": "Steaming rice cake closeup",
                "idempotency_key": "shot-01-v1",
                "wait": True,
                "wait_timeout_seconds": 1.0,
            },
        )
    )
    assert res1["status"] == "running"
    assert res1["job_id"].startswith("job_")
    assert res1["idempotent_replay"] is False
    assert len(mock.video_submissions) == 1
    assert mock.credits_balance == 980

    # 2. Replay with the EXACT same idempotency_key -> returns same job_id, NO new upstream submission, NO extra credit charge!
    res_replay = _extract_json(
        await server.call_tool(
            "flow_generate_video",
            {
                "prompt": "Steaming rice cake closeup",
                "idempotency_key": "shot-01-v1",
                "wait": False,
            },
        )
    )
    assert res_replay["job_id"] == res1["job_id"]
    assert res_replay["idempotent_replay"] is True
    assert len(mock.video_submissions) == 1
    assert mock.credits_balance == 980

    # 3. Poll via job_id until completion
    for _ in range(3):
        polled = _extract_json(
            await server.call_tool("flow_poll_media", {"job_id": res1["job_id"], "wait": False})
        )
    assert polled["status"] == "completed"
    assert polled["items"][0]["video_url"] == "https://storage.googleapis.com/fake/vid.mp4"

    # 4. Concurrent race with identical idempotency_key -> coalesced to 1 submission!
    r_a, r_b = await asyncio.gather(
        server.call_tool(
            "flow_generate_video",
            {"prompt": "Parallel retry race", "idempotency_key": "shot-02-race", "wait": False},
        ),
        server.call_tool(
            "flow_generate_video",
            {"prompt": "Parallel retry race", "idempotency_key": "shot-02-race", "wait": False},
        ),
    )
    ja, jb = _extract_json(r_a), _extract_json(r_b)
    assert ja["job_id"] == jb["job_id"]
    assert {ja["idempotent_replay"], jb["idempotent_replay"]} == {False, True}
    assert len(mock.video_submissions) == 2
    assert mock.credits_balance == 960

    # 5. Simulate abrupt client disconnect during wait -> job_id is still persisted & recoverable by idempotency_key!
    mock.poll_ready_after = 10
    call_task = asyncio.create_task(
        server.call_tool(
            "flow_generate_video",
            {
                "prompt": "Client drops connection mid-poll",
                "idempotency_key": "shot-03-drop",
                "wait": True,
                "wait_timeout_seconds": 15.0,
            },
        )
    )
    await asyncio.sleep(0.2)
    call_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call_task

    recovered = _extract_json(
        await server.call_tool("flow_poll_media", {"idempotency_key": "shot-03-drop", "wait": False})
    )
    assert recovered["job_id"].startswith("job_")
    assert len(recovered["media_ids"]) == 1


# ── P1-5: All 6 Structured Machine-Readable Error Codes ──────────────────────


@pytest.mark.parametrize(
    ("injected_exc", "expected_code", "expected_retriable"),
    [
        (AuthError("Google session expired"), ERR_AUTH_EXPIRED, False),
        (QuotaExceededError("0 credits left"), ERR_QUOTA_EXCEEDED, False),
        (RateLimitedError("Throttled"), ERR_RATE_LIMITED, True),
        (RpcError("503 backend unavailable", code=14), ERR_UPSTREAM_ERROR, True),
        (NotFoundError("Media does not exist"), ERR_NOT_FOUND, False),
    ],
)
async def test_p1_5_structured_error_codes_from_upstream(
    monkeypatch,
    tmp_path: Path,
    injected_exc: Exception,
    expected_code: str,
    expected_retriable: bool,
) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    mock.next_error = injected_exc
    server = create_server(client_factory=lambda: mock)  # type: ignore[arg-type]

    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("flow_account_usage", {})

    err_payload = json.loads(str(exc_info.value))
    assert err_payload["ok"] is False
    assert err_payload["error_code"] == expected_code
    assert err_payload["error"]["code"] == expected_code
    assert err_payload["error"]["retriable"] is expected_retriable
    assert isinstance(err_payload["error"]["message"], str) and err_payload["error"]["message"]


async def test_p1_5_invalid_input_and_rpc_classifier_mappings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    server = create_server(client_factory=lambda: mock)  # type: ignore[arg-type]

    # Empty prompt -> INVALID_INPUT
    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("flow_generate_video", {"prompt": "   "})
    payload = json.loads(str(exc_info.value))
    assert payload["error_code"] == ERR_INVALID_INPUT
    assert payload["error"]["retriable"] is False

    # Verify Boq gRPC status classifier mappings
    assert isinstance(_classify_rpc_error("Wz637c", 16, "UNAUTHENTICATED"), AuthError)
    assert isinstance(_classify_rpc_error("Wz637c", 8, "PUBLIC_ERROR_USER_QUOTA_REACHED"), QuotaExceededError)
    assert isinstance(
        _classify_rpc_error("Wz637c", 8, "PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED"),
        RateLimitedError,
    )
    assert isinstance(_classify_rpc_error("Wz637c", 5, "NOT_FOUND"), NotFoundError)


# ── P2-6: Boundary Tests Matching `docs/MCP_CONNECTOR_LIMITS.md` ──────────────


async def test_p2_6_documented_boundaries_and_retention(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    ft = FileTransferConfig(
        signer=FileLinkSigner.from_persistent_store(tmp_path / "gflow_home" / "secret.key"),
        base_url="https://flow-host.example.ts.net:8443",
    )
    server = create_server(client_factory=lambda: mock, file_transfer=ft)  # type: ignore[arg-type]

    # Boundary 1: Invalid image magic bytes -> INVALID_INPUT
    bad_b64 = base64.b64encode(b"NOT_A_REAL_IMAGE_HEADER_12345").decode("ascii")
    with pytest.raises(ToolError) as exc_fmt:
        await server.call_tool("flow_upload_image", {"image_base64": bad_b64})
    assert json.loads(str(exc_fmt.value))["error_code"] == ERR_INVALID_INPUT

    # Boundary 2: > 15 MiB inline base64 -> INVALID_INPUT
    oversized_inline = VALID_PNG + (b"\x00" * (15 * 1024 * 1024 + 64))
    oversized_b64 = base64.b64encode(oversized_inline).decode("ascii")
    with pytest.raises(ToolError) as exc_sz:
        await server.call_tool("flow_upload_image", {"image_base64": oversized_b64})
    assert json.loads(str(exc_sz.value))["error_code"] == ERR_INVALID_INPUT

    # Boundary 3: > 3 reference_media_ids -> INVALID_INPUT
    with pytest.raises(ToolError) as exc_refs:
        await server.call_tool(
            "flow_generate_video",
            {
                "prompt": "Too many refs",
                "reference_media_ids": ["m1", "m2", "m3", "m4"],
            },
        )
    assert json.loads(str(exc_refs.value))["error_code"] == ERR_INVALID_INPUT

    # Boundary 4: /files/ul > 20 MiB -> HTTP 413, invalid format -> HTTP 415
    ul_meta = ft.create_upload_link("proj-1111")
    ul_path = "/" + ul_meta["upload_url"].split(":8443/", 1)[1]
    asgi_app = server.http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="https://flow-host.example.ts.net:8443",
    ) as http:
        r_413 = await http.put(
            ul_path,
            content=b"x",
            headers={"Content-Type": "image/png", "Content-Length": str(20 * 1024 * 1024 + 1)},
        )
        assert r_413.status_code == 413

        # Because 413 rolled back single-use JTI, we can test 415 on the same link!
        r_415 = await http.put(
            ul_path,
            content=b"GIF89a_unsupported_gif",
            headers={"Content-Type": "image/gif"},
        )
        assert r_415.status_code == 415

    # Boundary 5: 7-day export retention pruning
    exp_dir = get_exports_dir()
    stale_mp4 = exp_dir / "gflow_scene_old.mp4"
    fresh_mp4 = exp_dir / "gflow_scene_fresh.mp4"
    stale_mp4.write_bytes(b"old")
    fresh_mp4.write_bytes(b"fresh")
    eight_days_ago = time.time() - (8 * 86400)
    os.utime(stale_mp4, (eight_days_ago, eight_days_ago))

    removed = prune_expired_exports()
    assert removed == 1
    assert not stale_mp4.exists()
    assert fresh_mp4.exists()


@pytest.mark.asyncio
async def test_addendum_invalid_model_and_params_return_invalid_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nonexistent models on all generation tools MUST return INVALID_INPUT (never NOT_FOUND)."""
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    server = create_server(
        profile="default",
        client_factory=lambda: mock,  # type: ignore[return-value]
        job_store=JobStore(tmp_path / "jobs.json"),
    )

    bad_calls = [
        ("flow_generate_video", {"prompt": "test", "model": "nonexistent_video_model_99"}),
        ("flow_generate_video", {"prompt": "test", "model": "veo_fake_999"}),
        ("flow_generate_video", {"prompt": "test", "model": "omni", "duration_seconds": 99}),
        ("flow_generate_video", {"prompt": "test", "model": "omni", "resolution": "8k"}),
        ("flow_extend_video", {"video_media_id": "vid-1", "prompt": "more", "model": "bogus_model"}),
        ("flow_generate_image", {"prompt": "cat", "model": "nonexistent_image_model"}),
        ("flow_generate_image", {"prompt": "cat", "model": "pro", "aspect_ratio": "99:1"}),
        ("flow_generate_video", {"prompt": "dog", "model": "omni", "aspect_ratio": "99:1"}),
        ("flow_upsample_video", {"video_media_id": "vid-1", "target": "8k_ultra_fake"}),
    ]

    for tool_name, args in bad_calls:
        with pytest.raises(ToolError) as exc_info:
            await server.call_tool(tool_name, args)
        payload = json.loads(str(exc_info.value))
        assert payload["ok"] is False
        assert payload["error_code"] == ERR_INVALID_INPUT, f"{tool_name}({args}) -> {payload}"
        assert payload["error"]["retriable"] is False
    assert len(mock.video_submissions) == 0


def test_addendum_public_aitk_error_wire_decoding_all_6_codes() -> None:
    """Verify binary/base64 `PublicAitkError` (`_.pta`) wire decoding across all 6 codes."""
    def _any_public_err(code_int: int) -> list[Any]:
        # Protobuf field 1 varint (`0x08` + varint)
        assert 0 <= code_int < 128
        b64 = base64.b64encode(bytes([0x08, code_int])).decode("ascii")
        return [
            "type.googleapis.com/google.internal.labs.aisandbox.proto.common.v1.PublicAitkError",
            b64,
        ]

    # 1. QUOTA_EXCEEDED: code=2 (`PUBLIC_ERROR_USER_QUOTA_REACHED` -> `CAI=`)
    e_quota = _classify_rpc_error("YhhmEf", 8, [8, "Resource exhausted", [_any_public_err(2)]])
    assert isinstance(e_quota, QuotaExceededError)
    assert e_quota.error_code == ERR_QUOTA_EXCEEDED
    assert e_quota.retriable is False
    assert e_quota.details["public_error"] == "PUBLIC_ERROR_USER_QUOTA_REACHED"

    # 2. RATE_LIMITED: code=51 (`PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED` -> `CDM=`) even under gRPC 8!
    e_conc = _classify_rpc_error("YhhmEf", 8, [8, "Resource exhausted", [_any_public_err(51)]])
    assert isinstance(e_conc, RateLimitedError)
    assert e_conc.error_code == ERR_RATE_LIMITED
    assert e_conc.retriable is True
    assert e_conc.details["public_error"] == "PUBLIC_ERROR_CONCURRENT_LIMIT_REACHED"

    # 3. INVALID_INPUT: code=1 (`PUBLIC_ERROR_UNSAFE_GENERATION` -> `CAE=`)
    e_unsafe = _classify_rpc_error("YhhmEf", 3, [3, "Invalid argument", [_any_public_err(1)]])
    assert e_unsafe.error_code == ERR_INVALID_INPUT  # type: ignore[attr-defined]
    assert e_unsafe.retriable is False  # type: ignore[attr-defined]
    assert e_unsafe.details["public_error"] == "PUBLIC_ERROR_UNSAFE_GENERATION"  # type: ignore[attr-defined]

    # 4. UPSTREAM_ERROR: code=27 (`PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT`)
    e_timeout = _classify_rpc_error("YhhmEf", 13, [13, "Internal", [_any_public_err(27)]])
    assert isinstance(e_timeout, RpcError)
    assert e_timeout.error_code == ERR_UPSTREAM_ERROR
    assert e_timeout.retriable is True

    # 5. AUTH_EXPIRED: gRPC 16 (`UNAUTHENTICATED`)
    e_auth = _classify_rpc_error("o30O0e", 16, [16, "Unauthenticated"])
    assert isinstance(e_auth, AuthError)
    assert e_auth.error_code == ERR_AUTH_EXPIRED
    assert e_auth.retriable is False

    # 6. NOT_FOUND: gRPC 5 (`NOT_FOUND`)
    e_nf = _classify_rpc_error("as29s", 5, [5, "Media not found"])
    assert isinstance(e_nf, NotFoundError)
    assert e_nf.error_code == ERR_NOT_FOUND
    assert e_nf.retriable is False


@pytest.mark.asyncio
async def test_addendum_account_usage_telemetry_limits_and_docs_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify live session_renewal telemetry, verified_limits, and MCP doc resources."""
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = HardenedMockClient()
    ft = FileTransferConfig(
        base_url="https://flow-host.example.ts.net:8443",
        signer=FileLinkSigner.from_persistent_store(tmp_path / "gflow_home" / "secret.key"),
    )
    server = create_server(
        profile="default",
        client_factory=lambda: mock,  # type: ignore[return-value]
        file_transfer=ft,
        keepalive_interval=540.0,
    )

    data = _extract_json(await server.call_tool("flow_account_usage", {}))
    assert data["session_renewal"]["enabled"] is True
    assert data["session_renewal"]["interval_seconds"] == 540
    assert data["session_renewal"]["on_failure_error_code"] == "AUTH_EXPIRED"
    assert data["verified_limits"]["max_concurrent_video_generations_per_account"] == 4
    assert data["verified_limits"]["max_concurrent_http_uploads"] == 4
    assert data["verified_limits"]["max_concurrent_http_downloads"] == 4
    assert data["verified_limits"]["inline_base64_upload_max_bytes"] == 15 * 1024 * 1024
    assert data["verified_limits"]["url_or_upload_link_max_bytes"] == 20 * 1024 * 1024

    resources = await server.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "gflow://docs/connector" in uris
    assert "gflow://docs/limits" in uris

