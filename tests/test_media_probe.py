"""Tests for the pure-Python MP4 dimension probe and measured-dimension reporting in MCP tools."""

from __future__ import annotations

import asyncio
import importlib
import struct
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gflow.media_probe import measure_mp4_dimensions
from gflow.types import MediaItem

_IDENTITY = (0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
_ROT90 = (0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000)


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def _box64(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 1) + box_type + struct.pack(">Q", 16 + len(payload)) + payload


def _box_to_end(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 0) + box_type + payload


def _ftyp() -> bytes:
    return _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")


def _tkhd(width: int, height: int, *, version: int = 0, matrix: tuple[int, ...] = _IDENTITY) -> bytes:
    vf = bytes([version, 0, 0, 3])
    if version == 1:
        times = struct.pack(">QQIIQ", 0, 0, 1, 0, 0)
    else:
        times = struct.pack(">IIIII", 0, 0, 1, 0, 0)
    rest = b"\x00" * 8 + struct.pack(">hhhH", 0, 0, 0, 0)
    mtx = struct.pack(">9i", *matrix)
    wh = struct.pack(">II", width << 16, height << 16)
    return _box(b"tkhd", vf + times + rest + mtx + wh)


def _hdlr(handler: bytes) -> bytes:
    return _box(b"hdlr", b"\x00" * 4 + b"\x00" * 4 + handler + b"\x00" * 12 + b"vh\x00")


def _visual_entry(entry_type: bytes, width: int, height: int) -> bytes:
    payload = b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 16 + struct.pack(">HH", width, height) + b"\x00" * 50
    return _box(entry_type, payload)


def _trak(width: int, height: int, handler: bytes | None = b"vide", **tkhd_kw: Any) -> bytes:
    body = _tkhd(width, height, **tkhd_kw)
    if handler is not None:
        body += _box(b"mdia", _hdlr(handler))
    return _box(b"trak", body)


def build_mp4(width: int, height: int, **kw: Any) -> bytes:
    moov = _box(b"moov", _box(b"mvhd", b"\x00" * 100) + _trak(width, height, **kw))
    return _ftyp() + moov + _box(b"mdat", b"\x00" * 64)


def test_minimal_ftyp_moov_trak_tkhd_portrait_and_landscape() -> None:
    portrait = _ftyp() + _box(b"moov", _box(b"trak", _tkhd(720, 1280)))
    landscape = _ftyp() + _box(b"moov", _box(b"trak", _tkhd(1280, 720)))
    assert measure_mp4_dimensions(portrait) == (720, 1280)
    assert measure_mp4_dimensions(landscape) == (1280, 720)


def test_full_layout_with_handler_and_mdat() -> None:
    assert measure_mp4_dimensions(build_mp4(720, 1280)) == (720, 1280)
    assert measure_mp4_dimensions(build_mp4(1920, 1080)) == (1920, 1080)


def test_tkhd_version_1() -> None:
    assert measure_mp4_dimensions(build_mp4(720, 1280, version=1)) == (720, 1280)
    assert measure_mp4_dimensions(build_mp4(1280, 720, version=1)) == (1280, 720)


def test_rotation_matrix_swaps_dimensions() -> None:
    assert measure_mp4_dimensions(build_mp4(1280, 720, matrix=_ROT90)) == (720, 1280)


def test_skips_audio_track_and_picks_video_track() -> None:
    moov = _box(b"moov", _trak(0, 0, handler=b"soun") + _trak(720, 1280, handler=b"vide"))
    assert measure_mp4_dimensions(_ftyp() + moov) == (720, 1280)


def test_64bit_largesize_box() -> None:
    moov = _box64(b"moov", _box(b"trak", _tkhd(720, 1280)))
    assert measure_mp4_dimensions(_ftyp() + moov) == (720, 1280)


def test_size_zero_box_extends_to_end() -> None:
    moov_last = _ftyp() + _box_to_end(b"moov", _box(b"trak", _tkhd(720, 1280)))
    assert measure_mp4_dimensions(moov_last) == (720, 1280)

    mdat_last = (
        _ftyp()
        + _box(b"moov", _box(b"trak", _tkhd(1280, 720)))
        + _box_to_end(b"mdat", b"\x00" * 128)
    )
    assert measure_mp4_dimensions(mdat_last) == (1280, 720)


@pytest.mark.parametrize("entry_type", [b"avc1", b"hvc1"])
def test_stsd_visual_sample_entry_fallback(entry_type: bytes) -> None:
    stsd = _box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + _visual_entry(entry_type, 720, 1280))
    trak = _box(b"trak", _box(b"mdia", _hdlr(b"vide") + _box(b"minf", _box(b"stbl", stsd))))
    assert measure_mp4_dimensions(_ftyp() + _box(b"moov", trak)) == (720, 1280)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"not an mp4 at all" * 50,
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"V" * 4096),
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        bytes(range(256)) * 16,
        b"\x00\x00\x00\x04moov" + b"\x00" * 32,
        _ftyp() + _box(b"moov", _box(b"mvhd", b"\x00" * 100)),
    ],
)
def test_garbage_returns_none(data: bytes) -> None:
    assert measure_mp4_dimensions(data) is None


def test_truncated_mp4_returns_none() -> None:
    full = build_mp4(720, 1280)
    assert measure_mp4_dimensions(full[:60]) is None


def test_media_item_dimensions_source_default() -> None:
    item = MediaItem(id="m1", project_id="p1")
    assert item.dimensions_source is None
    assert item.to_dict()["dimensions_source"] is None


def _load_hardening_module() -> ModuleType:
    for name in ("test_production_hardening", "tests.test_production_hardening"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    pytest.skip("tests/test_production_hardening.py helpers not importable in this pytest import mode")
    raise AssertionError("unreachable")


def _make_server(mock: Any, tmp_path: Path) -> Any:
    from gflow.mcp._filelink import FileLinkSigner, FileTransferConfig
    from gflow.mcp._jobs import JobStore
    from gflow.mcp.server import create_server

    ft = FileTransferConfig(
        signer=FileLinkSigner.from_persistent_store(tmp_path / "gflow_home" / "secret.key"),
        base_url="https://flow-host.example.ts.net:8443",
    )
    return create_server(
        client_factory=lambda: mock,
        file_transfer=ft,
        job_store=JobStore(tmp_path / "gflow_home" / "jobs.json"),
    )


def test_flow_get_media_measures_real_dimensions(monkeypatch, tmp_path: Path) -> None:
    hardening = _load_hardening_module()
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    portrait = build_mp4(720, 1280)

    class PortraitVideoMock(hardening.HardenedMockClient):  # type: ignore[name-defined,misc]
        async def get_media(self, media_id: str) -> MediaItem:
            return MediaItem(
                id=media_id,
                project_id="proj-1111",
                media_type="video",
                status="completed",
                mime_type="video/mp4",
                width=1280,
                height=720,
                dimensions_source="inferred_from_aspect",
                video_url="https://storage.googleapis.com/fake/vid.mp4",
            )

        async def download_bytes(self, url: str, *args: Any, **kwargs: Any) -> tuple[bytes, str]:
            return portrait, "video/mp4"

    async def main() -> None:
        server = _make_server(PortraitVideoMock(), tmp_path)

        plain = hardening._extract_json(await server.call_tool("flow_get_media", {"media_id": "vid-portrait"}))
        assert "measured_width" not in plain
        assert plain["dimensions_source"] == "inferred_from_aspect"

        verified = hardening._extract_json(
            await server.call_tool("flow_get_media", {"media_id": "vid-portrait", "verify_dimensions": True})
        )
        assert verified["measured_width"] == 720
        assert verified["measured_height"] == 1280
        assert verified["width"] == 720
        assert verified["height"] == 1280
        assert verified["dimensions_source"] == "measured_mp4"
        assert verified["dimension_mismatch"] is True

        checked = hardening._extract_json(
            await server.call_tool("flow_get_media", {"media_id": "vid-portrait", "include_checksum": True})
        )
        assert checked["file_size_bytes"] == len(portrait)
        assert checked["measured_width"] == 720
        assert checked["measured_height"] == 1280
        assert checked["dimensions_source"] == "measured_mp4"

    asyncio.run(main())


def test_flow_scene_export_reports_measured_dimensions(monkeypatch, tmp_path: Path) -> None:
    hardening = _load_hardening_module()
    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    portrait = build_mp4(720, 1280)

    class PortraitSceneMock(hardening.HardenedMockClient):  # type: ignore[name-defined,misc]
        async def export_scene_mp4(
            self,
            project_id: str | None,
            scene_id: str,
            *,
            output_path: str | Path | None = None,
            **kwargs: Any,
        ) -> tuple[str, Path | None]:
            self.scene_exports += 1
            assert output_path is not None
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(portrait)
            return "https://storage.googleapis.com/fake/scene_export.mp4", out

    async def main() -> None:
        mock = PortraitSceneMock()
        server = _make_server(mock, tmp_path)
        args = {"scene_id": "scene-portrait-01", "idempotency_key": "exp-portrait"}
        res = hardening._extract_json(await server.call_tool("flow_scene_export_mp4", args))
        assert res["measured_width"] == 720
        assert res["measured_height"] == 1280

        replay = hardening._extract_json(await server.call_tool("flow_scene_export_mp4", args))
        assert replay["idempotent_replay"] is True
        assert replay["measured_width"] == 720
        assert mock.scene_exports == 1

        # Same key, same scene, different local output_path: still a replay (output_path is not part of the fingerprint).
        again = hardening._extract_json(
            await server.call_tool(
                "flow_scene_export_mp4", {**args, "output_path": str(tmp_path / "elsewhere.mp4")}
            )
        )
        assert again["idempotent_replay"] is True
        assert mock.scene_exports == 1

        # Same key, different scene: rejected.
        with pytest.raises(ToolError):
            await server.call_tool("flow_scene_export_mp4", {**args, "scene_id": "scene-other-02"})
        assert mock.scene_exports == 1

    asyncio.run(main())


def _trak_full(*, handler: bytes | None, width: int, height: int, entry: tuple[bytes, int, int] | None = None,
               flags: int = 3, matrix: tuple[int, ...] = _IDENTITY, version: int = 0) -> bytes:
    tk = bytearray(_tkhd(width, height, matrix=matrix, version=version))
    tk[8 + 1 : 8 + 4] = flags.to_bytes(3, "big")  # box header(8) + version(1) -> flags
    mdia_body = b""
    if handler is not None:
        mdia_body += _hdlr(handler)
    if entry is not None:
        stsd = _box(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + _visual_entry(*entry))
        mdia_body += _box(b"minf", _box(b"stbl", stsd))
    return _box(b"trak", bytes(tk) + _box(b"mdia", mdia_body))


def test_cover_art_track_does_not_win_over_real_video_track() -> None:
    cover = _trak_full(handler=b"vide", width=512, height=512, flags=0, entry=(b"avc1", 512, 512))
    real = _trak_full(handler=b"vide", width=1080, height=1920, entry=(b"avc1", 1080, 1920))
    assert measure_mp4_dimensions(_ftyp() + _box(b"moov", cover + real)) == (1080, 1920)


def test_subtitle_track_with_truncated_hdlr_is_ignored_when_video_exists() -> None:
    sub = _trak_full(handler=None, width=1280, height=100)
    real = _trak_full(handler=b"vide", width=720, height=1280, entry=(b"avc1", 720, 1280))
    assert measure_mp4_dimensions(_ftyp() + _box(b"moov", sub + real)) == (720, 1280)


def test_stsd_fallback_respects_rotation_matrix_when_tkhd_size_is_zero() -> None:
    trak = _trak_full(handler=b"vide", width=0, height=0, entry=(b"avc1", 1920, 1080), matrix=_ROT90)
    assert measure_mp4_dimensions(_ftyp() + _box(b"moov", trak)) == (1080, 1920)


def test_unknown_tkhd_version_is_rejected() -> None:
    bad = bytearray(build_mp4(720, 1280))
    idx = bytes(bad).index(b"tkhd") + 4
    bad[idx] = 2
    assert measure_mp4_dimensions(bytes(bad)) is None
