"""Comprehensive tests for Issue #2: Omni 1.2 extend duration (3-10s), Scotty video upload, and own-footage video edit (`jIps6`)."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastmcp.exceptions import ToolError

from gflow._proto import (
    RPC_VIDEO_EDIT,
    RPC_VIDEO_EXTEND,
    RPC_VIDEO_TEXT,
    build_generate_video_req,
    parse_media_item,
    parse_video_upload_response,
    resolve_video_model_key,
)
from gflow._rpc import FlowHttpRpc, _classify_rpc_error
from gflow.cli.main import cli
from gflow.client import FlowClient
from gflow.exceptions import (
    ERR_INVALID_INPUT,
    ERR_RATE_LIMITED,
    RateLimitedError,
    ValidationError,
)
from gflow.mcp._filelink import FileLinkSigner, FileTransferConfig
from gflow.mcp._jobs import JobStore
from gflow.mcp.server import create_server
from gflow.media_probe import detect_video_mime, measure_mp4_duration
from gflow.types import AccountCredits, MediaItem, Project, UserAccount

FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"\x00" * 256)
FAKE_MOV = b"\x00\x00\x00\x14ftypqt  \x20\x05\x03\x00qt  " + (b"\x00" * 64)
FAKE_WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm" + (b"\x00" * 64)
FAKE_AVI = b"RIFF\x24\x00\x00\x00AVI LIST" + (b"\x00" * 64)
FAKE_HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
FAKE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_mp4_with_mvhd(duration_seconds: float, timescale: int = 1000, version: int = 0) -> bytes:
    ftyp = struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 0, b"mp42")
    dur_units = int(round(duration_seconds * timescale))
    if version == 0:
        mvhd_body = struct.pack(">4sIIII", b"\x00\x00\x00\x00", 0, 0, timescale, dur_units) + (b"\x00" * 80)
    else:
        mvhd_body = struct.pack(">4sQQIQ", b"\x01\x00\x00\x00", 0, 0, timescale, dur_units) + (b"\x00" * 80)
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    return ftyp + moov


def _extract_json(result: Any) -> dict[str, Any]:
    if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
        return result.structured_content
    texts = getattr(result, "content", result)
    return json.loads(texts[0].text)


# ── 1. Media probing: duration & video magic bytes ───────────────────────────


def test_measure_mp4_duration_v0_and_v1() -> None:
    assert measure_mp4_duration(_make_mp4_with_mvhd(4.0, version=0)) == 4.0
    assert measure_mp4_duration(_make_mp4_with_mvhd(15.04, timescale=24000, version=1)) == 15.04
    assert measure_mp4_duration(b"not an mp4") is None


def test_detect_video_mime_formats_and_rejections() -> None:
    assert detect_video_mime(FAKE_MP4) == ("video/mp4", ".mp4")
    assert detect_video_mime(FAKE_MOV) == ("video/quicktime", ".mov")
    assert detect_video_mime(FAKE_WEBM) == ("video/webm", ".webm")
    assert detect_video_mime(FAKE_AVI) == ("video/x-msvideo", ".avi")
    assert detect_video_mime(FAKE_HEIC) is None
    assert detect_video_mime(FAKE_PNG) is None
    assert detect_video_mime(b"short") is None


# ── 2. Gap 1: Extend duration protocol & 360p model resolution ───────────────


@pytest.mark.parametrize("dur", [3, 4, 8, 10])
def test_proto_extend_duration_3_to_10_and_360p(dur: int) -> None:
    rpc, req, _ = build_generate_video_req(
        "proj-1",
        "continue the tracking shot",
        model="omni",
        aspect_ratio="16:9",
        duration_seconds=dur,
        extend_video_media_id="vid-src-1",
    )
    assert rpc == RPC_VIDEO_EXTEND
    assert req[0][0][2] == f"macaron_cookie_extend_{dur}s"
    assert req[0][0][3] == 2  # 16:9

    rpc_360, req_360, _ = build_generate_video_req(
        "proj-1",
        "continue draft",
        model="omni",
        aspect_ratio="9:16",
        resolution="360p",
        duration_seconds=dur,
        extend_video_media_id="vid-src-1",
        valid_video_model_keys={f"macaron_cookie_extend_{dur}s", f"macaron_cookie_extend_{dur}s_360p"},
    )
    assert rpc_360 == RPC_VIDEO_EXTEND
    assert req_360[0][0][2] == f"macaron_cookie_extend_{dur}s_360p"
    assert req_360[0][0][3] == 1  # 9:16
    assert req_360[0][0][12] == [4]  # field 13 res_msg = 360p


@pytest.mark.parametrize("bad_dur", [0, 2, 11, 15, True, False])
def test_proto_extend_duration_rejects_out_of_range(bad_dur: Any) -> None:
    with pytest.raises(ValidationError):
        resolve_video_model_key("omni", mode="EXTEND_VIDEO", duration_seconds=bad_dur)


def test_proto_omni_360p_key_for_t2v() -> None:
    rpc, req, _ = build_generate_video_req(
        "proj-1",
        "draft clip",
        model="omni",
        resolution="360p",
        duration_seconds=6,
    )
    assert rpc == RPC_VIDEO_TEXT
    assert req[0][0][1] == "macaron_cookie_t2v_6s_360p"
    assert req[0][0][7] == [4]  # field 8 res_msg = 360p


# ── 3. Gap 2: EDIT_VIDEO (`jIps6`) wire schema & parser tests ────────────────


def test_proto_edit_video_wire_layout_matches_flow_dy_bsa() -> None:
    rpc, req, uuids = build_generate_video_req(
        "proj-1",
        "Change lighting to warm golden hour sunset",
        model="omni",
        aspect_ratio="9:16",
        resolution="360p",
        edit_video_media_id="vid-uploaded-1",
        edit_start_frame=0,
        edit_end_frame=96,
        edit_start_time_seconds=0.0,
        edit_end_time_seconds=4.0,
        reference_media_ids=["ref-img-1"],
        valid_video_model_keys={"macaron_cookie_edit", "macaron_cookie_edit_360p"},
    )
    assert rpc == RPC_VIDEO_EDIT == "jIps6"
    assert len(uuids) == 1
    item = req[0][0]
    # _.Dy field 1 (idx 0): uM src_vid -> field 2=mediaId, 3=startFrame, 4=endFrame, 6=startDur, 7=endDur
    assert item[0][1] == "vid-uploaded-1"
    assert item[0][2] == 0
    assert item[0][3] == 96
    assert item[0][5] == [0]
    assert item[0][6] == [4]
    # _.Dy field 2 (idx 1): text_input
    assert item[1][2] == [[["Change lighting to warm golden hour sunset"]]]
    # _.Dy field 3 (idx 2): model_key
    assert item[2] == "macaron_cookie_edit_360p"
    # _.Dy field 4 (idx 3): aspect_ratio (1 = 9:16)
    assert item[3] == 1
    # _.Dy field 5 (idx 4): dest metadata (CRITICAL: field 5 in _.Dy, NOT field 6 as in _.Hy!)
    assert item[4][4] == uuids[0]
    assert len(item) > 5 and item[5] is None
    # _.Dy field 9 (idx 8): references
    assert item[8] == [[None, "ref-img-1"]]
    # _.Dy field 13 (idx 12): res_msg
    assert item[12] == [4]


def test_proto_edit_video_model_resolution_tiers_and_veo_rejection() -> None:
    assert resolve_video_model_key("omni", mode="EDIT_VIDEO") == ("macaron_cookie_edit", False)
    assert resolve_video_model_key("omni", mode="EDIT_VIDEO", resolution="360p") == (
        "macaron_cookie_edit_360p",
        False,
    )
    assert resolve_video_model_key("omni", mode="EDIT_VIDEO", service_tier=1) == (
        "macaron_cookie_edit_entry",
        False,
    )
    assert resolve_video_model_key("omni", mode="EDIT_VIDEO", resolution="360p", service_tier=1) == (
        "macaron_cookie_edit_entry_360p",
        False,
    )
    assert resolve_video_model_key("abra_edit", mode="EDIT_VIDEO") == ("abra_edit", False)
    for veo_alias in ("fast", "quality", "lite"):
        with pytest.raises(ValidationError):
            resolve_video_model_key(veo_alias, mode="EDIT_VIDEO")


def test_parse_uploaded_video_scotty_and_live_as29s_payload() -> None:
    scotty_json = {
        "mediaId": "76d63007-1669-44c3-bce4-3f49b03d6a28",
        "media": {
            "name": "76d63007-1669-44c3-bce4-3f49b03d6a28",
            "projectId": "fd684391-6da2-4cf9-bcc1-5ebf0599bcad",
            "workflowId": "3567d9ee-f229-4798-b727-7e18633fbf4b",
            "mediaMetadata": {
                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"}
            },
            "video": {"dimensions": {"width": 1280, "height": 720, "length": "4s"}},
        },
        "workflow": {"name": "3567d9ee-f229-4798-b727-7e18633fbf4b"},
    }
    initial = parse_video_upload_response(scotty_json)
    assert initial is not None
    assert initial.id == "76d63007-1669-44c3-bce4-3f49b03d6a28"
    assert initial.workflow_id == "3567d9ee-f229-4798-b727-7e18633fbf4b"
    assert initial.status == "running"
    assert (initial.width, initial.height) == (1280, 720)
    assert initial.duration_seconds == 4.0
    assert initial.dimensions_source == "reported"

    # Real captured as29s payload once Google Flow finishes transcoding an uploaded video:
    live_as29s = [
        "76d63007-1669-44c3-bce4-3f49b03d6a28",
        "fd684391-6da2-4cf9-bcc1-5ebf0599bcad",
        "3567d9ee-f229-4798-b727-7e18633fbf4b",
        None,
        None,
        [
            [1795527822, 919387000],
            None,
            None,
            None,
            None,
            None,
            [None, None, None, None, 1],
            None,
            [3],
            1,
            "https://flow-content.google/image/76d63007?Sig=thumb",
            None,
            None,
            1342295,
        ],
        None,
        [
            None,
            [1280, 720, [4, 10000000]],
            None,
            None,
            ["", None, "https://flow-content.google/video/76d63007?Sig=vid"],
            [
                None,
                [
                    "https://flow-content.google/video/76d63007?Sig=vid",
                    "https://flow-content.google/image/76d63007?Sig=thumb",
                ],
            ],
        ],
    ]
    ready = parse_media_item(live_as29s)
    assert ready is not None
    assert ready.media_type == "video"
    assert ready.status == "completed"
    assert (ready.width, ready.height) == (1280, 720)
    assert ready.dimensions_source == "reported"
    assert ready.duration_seconds == 4.01
    assert ready.video_url == "https://flow-content.google/video/76d63007?Sig=vid"
    assert ready.thumbnail_url == "https://flow-content.google/image/76d63007?Sig=thumb"
    assert ready.download_url == ready.video_url


# ── 4. Scotty HTTP transport & ErrorInfo classification ──────────────────────


async def test_scotty_resumable_video_upload_wire_protocol() -> None:
    seen: list[tuple[str, dict[str, str], bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            html = '<script>window.WIZ_global_data={"SNlM0e":"xsrf_tok:99","FdrFJe":"111","cfb2h":"boq_1"};</script>'
            return httpx.Response(200, text=html)
        hdrs = {k.lower(): v for k, v in request.headers.items()}
        seen.append((str(request.url), hdrs, request.content))
        if hdrs.get("x-goog-upload-command") == "start":
            return httpx.Response(
                200,
                headers={
                    "X-Goog-Upload-Status": "active",
                    "X-Goog-Upload-URL": "https://flow.google.com/upload/v1/flow/upload/video/proj-1?upload_id=u123",
                    "X-Goog-Upload-Chunk-Granularity": "1048576",
                },
            )
        assert hdrs.get("x-goog-upload-command") == "upload, finalize"
        return httpx.Response(
            200,
            headers={"X-Goog-Upload-Status": "final"},
            json={"mediaId": "vid-scotty-001", "media": {"name": "vid-scotty-001"}},
        )

    rpc = FlowHttpRpc(httpx.Cookies())
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await rpc.upload_video_scotty(
        "proj-1",
        FAKE_MP4,
        file_name="my clip.mp4",
        mime_type="video/mp4",
        trim_start_seconds=1.5,
        trim_end_seconds=9.5,
    )
    await rpc.close()

    assert res["mediaId"] == "vid-scotty-001"
    assert len(seen) == 2
    start_url, start_h, start_body = seen[0]
    assert start_url == "https://flow.google.com/upload/v1/flow/upload/video/proj-1"
    assert start_h["x-framework-xsrf-token"] == "xsrf_tok:99"
    assert start_h["slug"] == "my%20clip.mp4"
    assert start_h["x-goog-upload-protocol"] == "resumable"
    assert start_h["x-goog-upload-command"] == "start"
    assert start_h["x-goog-upload-header-content-length"] == str(len(FAKE_MP4))
    assert start_h["x-goog-upload-header-content-type"] == "video/mp4"
    assert start_h["x-trim-start"] == "1500"
    assert start_h["x-trim-end"] == "9500"
    assert start_body == b""

    up_url, up_h, up_body = seen[1]
    assert "upload_id=u123" in up_url
    assert up_h["x-goog-upload-offset"] == "0"
    assert up_body == FAKE_MP4


def test_error_info_public_error_name_classification() -> None:
    err_unusual = _classify_rpc_error(
        "jIps6",
        7,
        [7, None, [["type.googleapis.com/google.rpc.ErrorInfo", ["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]]]],
    )
    assert isinstance(err_unusual, RateLimitedError)
    assert err_unusual.error_code == ERR_RATE_LIMITED
    assert err_unusual.retriable is True

    err_dur = _classify_rpc_error(
        "jIps6",
        3,
        [3, None, [["type.googleapis.com/google.rpc.ErrorInfo", ["PUBLIC_ERROR_VIDEO_DURATION_TOO_LONG"]]]],
    )
    assert isinstance(err_dur, ValidationError)
    assert err_dur.error_code == ERR_INVALID_INPUT


# ── 5. FlowClient SDK tests (extend duration, upload_video, edit_video) ──────


class _FakeGuardedRpc:
    def __init__(self) -> None:
        self.scotty_calls: list[dict[str, Any]] = []
        self.rpc_calls: list[tuple[str, Any]] = []
        self._on_auth_expired = None

    async def bootstrap(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def call(self, rpc_id: str, payload: Any, **kwargs: Any) -> Any:
        self.rpc_calls.append((rpc_id, payload))
        if rpc_id == "HTrJv":
            # Minimal catalog listing omni extend 3..10s and edit keys
            usages = [
                ["macaron_cookie_edit"],
                ["macaron_cookie_edit_360p"],
                ["macaron_cookie_edit_entry"],
                ["macaron_cookie_edit_entry_360p"],
                ["macaron_cookie_extend_8s"],
                ["macaron_cookie_extend_10s"],
                ["macaron_cookie_extend_10s_360p"],
            ]
            return [[None, None, None, None, [["Omni 1.2 Flash", usages, None, "macaron_cookie"]]]]
        if rpc_id == "as29s":
            mid = payload[0]
            return [
                mid,
                "11111111-2222-3333-4444-555555555555",
                "wf-1",
                None,
                None,
                [[1795527822], None, None, None, None, None, None, None, [3]],
                None,
                [
                    None,
                    [720, 1280, [15, 40000000]],
                    None,
                    None,
                    ["", None, f"https://flow-content.google/video/{mid}"],
                ],
            ]
        return []

    async def upload_video_scotty(self, project_id: str, video_bytes: bytes, **kwargs: Any) -> dict[str, Any]:
        self.scotty_calls.append({"project_id": project_id, "size": len(video_bytes), **kwargs})
        return {
            "mediaId": "uploaded-vid-123",
            "media": {
                "name": "uploaded-vid-123",
                "projectId": project_id,
                "workflowId": "wf-up-1",
                "mediaMetadata": {"mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_PENDING"}},
                "video": {"dimensions": {"width": 720, "height": 1280, "length": "4s"}},
            },
        }


async def test_client_extend_duration_and_edit_video_local_upload(tmp_path: Path) -> None:
    rpc = _FakeGuardedRpc()
    client = FlowClient(rpc, storage_state_path=tmp_path / "storage.json")  # type: ignore[arg-type]
    captured_guarded: list[tuple[str, str, Any]] = []

    async def _fake_guarded(action: str, pid: str, build_fn: Any) -> Any:
        r_id, r_payload = build_fn("tok-test")
        captured_guarded.append((action, r_id, r_payload))
        return [[["out-vid-1", pid, "wf-out", None, None, None, None, None]]]

    client._call_guarded = _fake_guarded  # type: ignore[method-assign]

    pid = "11111111-2222-3333-4444-555555555555"

    # 1. extend_video passes duration_seconds=10 -> macaron_cookie_extend_10s
    await client.extend_video(pid, "vid-base", "camera cranes upward", model="omni", duration_seconds=10, wait=False)
    action_ext, rpc_ext, payload_ext = captured_guarded[-1]
    assert action_ext == "VIDEO_GENERATION"
    assert rpc_ext == RPC_VIDEO_EXTEND
    assert payload_ext[0][0][2] == "macaron_cookie_extend_10s"

    # 2. Default extend_video keeps 8s
    await client.extend_video(pid, "vid-base", "continue default", model="omni", wait=False)
    assert captured_guarded[-1][2][0][0][2] == "macaron_cookie_extend_8s"

    # 3. edit_video with local MP4 file auto-uploads via Scotty, preserves 9:16 portrait from source,
    #    and clamps 15.04s source clip to 15.0s edit window!
    local_mp4 = tmp_path / "portrait_clip.mp4"
    local_mp4.write_bytes(_make_mp4_with_mvhd(15.04))
    await client.edit_video(pid, local_mp4, "Switch to neon cyberpunk rain", model="omni", wait=False)

    assert len(rpc.scotty_calls) == 1
    assert rpc.scotty_calls[0]["file_name"] == "portrait_clip.mp4"
    action_ed, rpc_ed, payload_ed = captured_guarded[-1]
    assert action_ed == "VIDEO_GENERATION"
    assert rpc_ed == RPC_VIDEO_EDIT
    ed_item = payload_ed[0][0]
    assert ed_item[0][1] == "uploaded-vid-123"
    assert ed_item[0][2] == 0
    assert ed_item[0][3] == 360  # 15.0s * 24fps (clamped from 15.04s)
    assert ed_item[0][5] == [0]
    assert ed_item[0][6] == [15]
    assert ed_item[2] == "macaron_cookie_edit"
    assert ed_item[3] == 1  # auto-preserved 9:16 portrait!


# ── 6. MCP server & /files/ul video upload + extend duration + edit video ────


class _OmniMockClient:
    def __init__(self) -> None:
        self.video_uploads: list[dict[str, Any]] = []
        self.extend_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self._rpc = self

    async def __aenter__(self) -> _OmniMockClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def whoami(self) -> UserAccount:
        return UserAccount(user_id="u1", email="demo@example.com")

    async def get_credits(self) -> AccountCredits:
        return AccountCredits(total_credits=20000, subscription_credits=20000)

    async def resolve_project_id(self, pid: str | None = None) -> str:
        return pid or "proj-1111"

    async def upload_video(
        self,
        project_id: str | None,
        source: str | Path | bytes,
        **kwargs: Any,
    ) -> MediaItem:
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        self.video_uploads.append({"size": len(raw), **kwargs})
        return MediaItem(
            id=f"up-vid-{len(self.video_uploads):03d}",
            project_id=project_id or "proj-1111",
            media_type="video",
            status="completed",
            mime_type=kwargs.get("mime_type") or "video/mp4",
            width=1280,
            height=720,
            duration_seconds=4.0,
            video_url="https://flow-content.google/video/up-vid",
        )

    async def extend_video(self, project_id: str | None, video_media_id: str, prompt: str, **kwargs: Any) -> list[MediaItem]:
        self.extend_calls.append({"video_media_id": video_media_id, "prompt": prompt, **kwargs})
        return [
            MediaItem(
                id=f"ext-{len(self.extend_calls):03d}",
                project_id=project_id or "proj-1111",
                media_type="video",
                status="completed",
                duration_seconds=float(kwargs.get("duration_seconds", 8)),
                video_url="https://flow-content.google/video/ext",
            )
        ]

    async def edit_video(self, project_id: str | None, video: str | Path, prompt: str, **kwargs: Any) -> list[MediaItem]:
        self.edit_calls.append({"video": str(video), "prompt": prompt, **kwargs})
        return [
            MediaItem(
                id=f"edit-{len(self.edit_calls):03d}",
                project_id=project_id or "proj-1111",
                media_type="video",
                status="completed",
                duration_seconds=4.0,
                video_url="https://flow-content.google/video/edit",
            )
        ]

    async def poll_media(self, media_ids: list[str], **kwargs: Any) -> list[MediaItem]:
        return [
            MediaItem(
                id=mid,
                project_id="proj-1111",
                media_type="video",
                status="completed",
                mime_type="video/mp4",
                video_url=f"https://flow-content.google/video/{mid}",
            )
            for mid in media_ids
        ]


async def test_mcp_extend_duration_upload_video_and_edit_video(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = _OmniMockClient()
    ft = FileTransferConfig(
        signer=FileLinkSigner.from_persistent_store(tmp_path / "gflow_home" / "secret.key"),
        base_url="https://flow-host.example.ts.net:8443",
    )
    server = create_server(
        client_factory=lambda: mock,  # type: ignore[arg-type]
        file_transfer=ft,
        job_store=JobStore(tmp_path / "gflow_home" / "jobs.json"),
    )

    tools = {t.name for t in await server.list_tools()}
    assert "flow_upload_video" in tools
    assert "flow_edit_video" in tools
    assert "flow_extend_video" in tools

    # 1. flow_extend_video forwards duration_seconds=10 & enforces idempotency param fingerprint
    ext_res = _extract_json(
        await server.call_tool(
            "flow_extend_video",
            {
                "video_media_id": "vid-001",
                "prompt": "Pan right to reveal sunset",
                "model": "omni",
                "duration_seconds": 10,
                "idempotency_key": "ext-key-1",
                "wait": False,
            },
        )
    )
    assert ext_res["job_id"].startswith("job_")
    assert mock.extend_calls[-1]["duration_seconds"] == 10

    # Same key + different duration -> rejected!
    with pytest.raises(ToolError) as exc_fp:
        await server.call_tool(
            "flow_extend_video",
            {
                "video_media_id": "vid-001",
                "prompt": "Pan right to reveal sunset",
                "model": "omni",
                "duration_seconds": 6,
                "idempotency_key": "ext-key-1",
                "wait": False,
            },
        )
    assert ERR_INVALID_INPUT in str(exc_fp.value)

    # Out-of-range duration on flow_extend_video -> INVALID_INPUT
    with pytest.raises(ToolError) as exc_dur:
        await server.call_tool(
            "flow_extend_video",
            {"video_media_id": "vid-001", "prompt": "Too long", "model": "omni", "duration_seconds": 11},
        )
    assert json.loads(str(exc_dur.value))["error_code"] == ERR_INVALID_INPUT

    # 2. flow_upload_video via base64
    up_res = _extract_json(
        await server.call_tool(
            "flow_upload_video",
            {
                "video_base64": "data:video/mp4;base64," + base64.b64encode(FAKE_MP4).decode("ascii"),
                "file_name": "own_footage.mp4",
            },
        )
    )
    assert up_res["media_id"] == "up-vid-001"
    assert "download_link" in up_res

    # Bad magic bytes on flow_upload_video -> INVALID_INPUT
    with pytest.raises(ToolError) as exc_bad:
        await server.call_tool(
            "flow_upload_video",
            {"video_base64": base64.b64encode(FAKE_PNG).decode("ascii")},
        )
    assert json.loads(str(exc_bad.value))["error_code"] == ERR_INVALID_INPUT

    # 3. flow_create_upload_link(kind="video") + /files/ul/{token} binary PUT
    ul_meta = _extract_json(
        await server.call_tool("flow_create_upload_link", {"kind": "video", "label": "Raw 10s footage"})
    )
    assert ul_meta["kind"] == "video"
    assert ul_meta["max_upload_bytes"] == 100 * 1024 * 1024
    ul_path = "/" + ul_meta["upload_url"].split(":8443/", 1)[1]

    asgi_app = server.http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="https://flow-host.example.ts.net:8443",
    ) as http:
        r_page = await http.get(ul_path)
        assert r_page.status_code == 200
        assert "Upload Video" in r_page.text
        assert "video/mp4" in r_page.text

        # Oversized (> 100 MiB) -> 413 (rolls back JTI)
        r_413 = await http.put(
            ul_path,
            content=b"x",
            headers={"Content-Type": "video/mp4", "Content-Length": str(100 * 1024 * 1024 + 1)},
        )
        assert r_413.status_code == 413

        # Image bytes sent to video upload link -> 415 (rolls back JTI)
        r_415 = await http.put(
            ul_path,
            content=FAKE_PNG,
            headers={"Content-Type": "image/png"},
        )
        assert r_415.status_code == 415

        # Valid MP4 binary PUT -> 200 & commits JTI
        r_ok = await http.put(
            f"{ul_path}?filename=camera_clip.mp4&trim_start=0&trim_end=8",
            content=FAKE_MP4,
            headers={"Content-Type": "video/mp4"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["media_id"] == "up-vid-002"
        assert mock.video_uploads[-1]["trim_start_seconds"] == 0.0
        assert mock.video_uploads[-1]["trim_end_seconds"] == 8.0

        # Replay after commit -> 403
        r_replay = await http.put(ul_path, content=FAKE_MP4, headers={"Content-Type": "video/mp4"})
        assert r_replay.status_code == 403

    # 4. flow_edit_video with uploaded media_id + idempotency
    ed_res = _extract_json(
        await server.call_tool(
            "flow_edit_video",
            {
                "video_media_id": "up-vid-002",
                "prompt": "Make the lighting moody blue moonlight",
                "model": "omni",
                "start_time_seconds": 0.0,
                "end_time_seconds": 8.0,
                "idempotency_key": "edit-key-1",
                "wait": False,
            },
        )
    )
    assert ed_res["job_id"].startswith("job_")
    assert mock.edit_calls[-1]["video"] == "up-vid-002"
    assert mock.edit_calls[-1]["end_time_seconds"] == 8.0

    # Invalid edit window (< 3s) or Veo model -> INVALID_INPUT
    for bad_args in (
        {"video_media_id": "up-vid-002", "prompt": "edit", "model": "fast"},
        {"video_media_id": "up-vid-002", "prompt": "edit", "start_time_seconds": 0.0, "end_time_seconds": 2.0},
        {"video_media_id": "up-vid-002", "prompt": "edit", "start_time_seconds": 0.0, "end_time_seconds": 16.0},
    ):
        with pytest.raises(ToolError) as exc_ed:
            await server.call_tool("flow_edit_video", bad_args)
        assert json.loads(str(exc_ed.value))["error_code"] == ERR_INVALID_INPUT


async def test_mcp_upload_video_restricted_security_guards(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    mock = _OmniMockClient()
    server = create_server(restricted=True, client_factory=lambda: mock)  # type: ignore[arg-type]

    real_file = tmp_path / "clip.mp4"
    real_file.write_bytes(FAKE_MP4)
    missing_file = tmp_path / "missing.mp4"

    # file_path rejected identically for existing and missing files without leaking path
    msgs: list[str] = []
    for p in (real_file, missing_file):
        with pytest.raises(ToolError) as exc_info:
            await server.call_tool("flow_upload_video", {"file_path": str(p)})
        msg = str(exc_info.value)
        assert "file_path is disabled on the remote HTTP transport" in msg
        assert str(tmp_path) not in msg
        msgs.append(msg)
    assert msgs[0] == msgs[1]

    # video_url pointing at loopback/metadata rejected by SSRF guard
    with pytest.raises(ToolError) as exc_ssrf:
        await server.call_tool("flow_upload_video", {"video_url": "https://127.0.0.1/secret.mp4"})
    assert "video_url fetch failed: blocked_address" in str(exc_ssrf.value)

    # flow_edit_video passes allow_local_paths=False so a host path passed as video_media_id is never uploaded from disk
    await server.call_tool(
        "flow_edit_video",
        {"video_media_id": str(real_file), "prompt": "change lighting", "wait": False},
    )
    assert mock.edit_calls[-1]["allow_local_paths"] is False
    assert mock.video_uploads == []


# ── 7. CLI tests: extend --duration, upload-video, edit ──────────────────────


def test_cli_extend_duration_upload_video_and_edit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "cli_home"))
    mock = _OmniMockClient()
    monkeypatch.setattr("gflow.cli.main.FlowClient.from_storage", lambda **kwargs: mock)

    runner = CliRunner()

    # 1. gflow extend --model omni --duration 10 --resolution 360p
    res_ext = runner.invoke(
        cli,
        ["--json", "extend", "vid-001", "Camera pulls back", "--model", "omni", "--duration", "10", "--resolution", "360p"],
    )
    assert res_ext.exit_code == 0, res_ext.output
    assert mock.extend_calls[-1]["duration_seconds"] == 10
    assert mock.extend_calls[-1]["resolution"] == "360p"

    # 2. gflow upload-video
    clip_path = tmp_path / "sample.mp4"
    clip_path.write_bytes(FAKE_MP4)
    res_up = runner.invoke(
        cli,
        ["--json", "upload-video", str(clip_path), "--trim-start", "1", "--trim-end", "5"],
    )
    assert res_up.exit_code == 0, res_up.output
    up_json = json.loads(res_up.output)
    assert up_json["id"] == "up-vid-001"
    assert mock.video_uploads[-1]["trim_start_seconds"] == 1.0
    assert mock.video_uploads[-1]["trim_end_seconds"] == 5.0

    # 3. gflow edit
    res_ed = runner.invoke(
        cli,
        [
            "--json",
            "edit",
            "up-vid-001",
            "Shift to dramatic noir lighting",
            "--model",
            "omni",
            "--resolution",
            "360p",
            "--start-time",
            "0",
            "--end-time",
            "4",
        ],
    )
    assert res_ed.exit_code == 0, res_ed.output
    ed_json = json.loads(res_ed.output)
    assert ed_json[0]["id"] == "edit-001"
    assert mock.edit_calls[-1]["video"] == "up-vid-001"
    assert mock.edit_calls[-1]["resolution"] == "360p"
    assert mock.edit_calls[-1]["end_time_seconds"] == 4.0


# ── 8. PR #3 review hardening regressions ────────────────────────────────────


async def test_review_hardening_duration_rollover_and_soft_wait_upload_route(monkeypatch, tmp_path: Path) -> None:
    from gflow._proto import duration_to_pb

    # Nanosecond rounding rollover (3.9999999999 -> [4], never [3, 1_000_000_000])
    assert duration_to_pb(3.9999999999) == [4]

    # Entry-tier edit_video rejects explicit >10s window & preserves start_time when src duration unknown
    rpc = _FakeGuardedRpc()
    client = FlowClient(rpc, storage_state_path=tmp_path / "storage.json")  # type: ignore[arg-type]
    captured: list[tuple[str, Any]] = []

    async def _fake_guarded(_action: str, pid: str, build_fn: Any) -> Any:
        r_id, r_payload = build_fn("tok-test")
        captured.append((r_id, r_payload))
        return [[["out-vid-2", pid, "wf-out", None, None, None, None, None]]]

    client._call_guarded = _fake_guarded  # type: ignore[method-assign]
    client._cached_service_tier = 1
    with pytest.raises(ValidationError, match="10s"):
        await client.edit_video(
            "11111111-2222-3333-4444-555555555555",
            "unknown-vid",
            "relight",
            start_time_seconds=0.0,
            end_time_seconds=12.0,
            wait=False,
        )

    # When get_media has no duration (e.g. raises NotFoundError), explicit start_time_seconds is still preserved
    async def _missing_media(_mid: str) -> MediaItem:
        raise ValidationError("no metadata")

    client.get_media = _missing_media  # type: ignore[method-assign]
    await client.edit_video(
        "11111111-2222-3333-4444-555555555555",
        "unknown-vid",
        "relight",
        start_time_seconds=2.0,
        wait=False,
    )
    assert captured[-1][0] == RPC_VIDEO_EDIT
    assert captured[-1][1][0][0][0][2] == 48  # start_frame = 2.0 * 24fps
    assert captured[-1][1][0][0][0][5] == [2]  # start_time Duration = 2s

    # Boolean / NaN guards on numeric parameters
    for bad_kw in (
        {"count": True},
        {"start_time_seconds": float("nan")},
        {"end_time_seconds": float("inf")},
    ):
        with pytest.raises(ValidationError):
            await client.edit_video(
                "11111111-2222-3333-4444-555555555555",
                "unknown-vid",
                "relight",
                wait=False,
                **bad_kw,  # type: ignore[arg-type]
            )

    # /files/ul/{token} with wait=0 returns 200 + media_id + media_status='running' without discarding media_id
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "ul_home"))
    mock = _OmniMockClient()

    async def _pending_upload(project_id: str | None, source: Any, **kwargs: Any) -> MediaItem:
        return MediaItem(
            id="up-pending-999",
            project_id=project_id or "proj-1111",
            media_type="video",
            status="running",
            mime_type="video/mp4",
        )

    mock.upload_video = _pending_upload  # type: ignore[method-assign]
    ft = FileTransferConfig(
        signer=FileLinkSigner.from_persistent_store(tmp_path / "ul_home" / "secret.key"),
        base_url="https://flow-host.example.ts.net:8443",
    )
    server = create_server(client_factory=lambda: mock, file_transfer=ft)  # type: ignore[arg-type]
    ul_meta = _extract_json(await server.call_tool("flow_create_upload_link", {"kind": "video"}))
    ul_path = "/" + ul_meta["upload_url"].split(":8443/", 1)[1]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.http_app()),
        base_url="https://flow-host.example.ts.net:8443",
    ) as http:
        r = await http.put(
            f"{ul_path}?filename=big.mp4&wait=0",
            content=FAKE_MP4,
            headers={"Content-Type": "video/mp4"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["media_id"] == "up-pending-999"
        assert body["media_status"] == "running"
