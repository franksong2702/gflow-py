"""Wire-level 9:16 vs 16:9 regression tests across every video mode x model tier (portrait fix)."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from gflow._proto import (
    RPC_VIDEO_EXPERIMENTAL,
    build_generate_video_req,
    resolve_video_aspect,
    resolve_video_model_key,
)
from gflow.exceptions import ValidationError

_MODES: dict[str, dict[str, Any]] = {
    "TEXT": {},
    "START_FRAME": {"start_frame_media_id": "f1"},
    "START_END_FRAMES": {"start_frame_media_id": "f1", "end_frame_media_id": "f2"},
    "REFERENCES": {"reference_media_ids": ["r1"]},
    "EXTEND_VIDEO": {"extend_video_media_id": "v1"},
}
_MODELS = ("omni", "fast", "quality", "lite")

# mode -> (index of videoModelKey, index of aspect-ratio field) inside payload[0][0] (0-offset JSPB)
_LAYOUT: dict[str, tuple[int, int]] = {
    "TEXT": (1, 2),
    "START_FRAME": (1, 2),
    "START_END_FRAMES": (1, 2),
    "REFERENCES": (2, 3),
    "EXTEND_VIDEO": (2, 3),
}
_EXPERIMENTAL_LAYOUT = (1, 11)

_OMNI_BASES = {
    "TEXT": "macaron_cookie_t2v_4s",
    "START_FRAME": "macaron_cookie_i2v_4s",
    "START_END_FRAMES": "macaron_cookie_i2v_fl_4s",
    "REFERENCES": "macaron_cookie_r2v_4s",
    "EXTEND_VIDEO": "macaron_cookie_extend_4s",
}


def _build(model: str, mode: str, aspect_ratio: str) -> tuple[Any, Any]:
    kwargs = dict(_MODES[mode])
    if model == "omni":
        kwargs["duration_seconds"] = 4
    rpc_id, payload, _ = build_generate_video_req(
        "p1", "a corgi surfing", model=model, aspect_ratio=aspect_ratio, seed=7, **kwargs
    )
    item = payload[0][0]
    key_idx, asp_idx = _EXPERIMENTAL_LAYOUT if rpc_id == RPC_VIDEO_EXPERIMENTAL else _LAYOUT[mode]
    return item[key_idx], item[asp_idx]


@pytest.mark.parametrize("model", _MODELS)
@pytest.mark.parametrize("mode", list(_MODES))
def test_portrait_and_landscape_differ_on_wire(mode: str, model: str) -> None:
    try:
        p_key, p_asp = _build(model, mode, "9:16")
        l_key, l_asp = _build(model, mode, "16:9")
    except ValidationError as exc:
        pytest.skip(f"{model}/{mode} unsupported: {exc}")

    assert isinstance(p_asp, int) and isinstance(l_asp, int)
    assert p_asp != l_asp, f"{model}/{mode}: 9:16 and 16:9 encode the same aspect value {p_asp}"
    assert p_asp != 0, f"{model}/{mode}: 9:16 must not encode as 0"
    assert isinstance(p_key, str) and isinstance(l_key, str)
    if model in ("omni", "fast", "quality"):
        assert "_portrait" in p_key, f"{model}/{mode}: portrait key {p_key!r} lacks '_portrait'"
        assert "_portrait" not in l_key
    elif p_key != l_key:
        assert "_portrait" in p_key


@pytest.mark.parametrize(("mode", "base"), sorted(_OMNI_BASES.items()))
def test_omni_portrait_key_offline(mode: str, base: str) -> None:
    assert resolve_video_model_key("omni", mode=mode, is_portrait=True, duration_seconds=4) == (
        f"{base}_portrait",
        False,
    )
    # Landscape behaviour unchanged: bare key, no "_landscape" suffix.
    assert resolve_video_model_key("omni", mode=mode, is_portrait=False, duration_seconds=4) == (base, False)


@pytest.mark.parametrize(("mode", "base"), sorted(_OMNI_BASES.items()))
def test_omni_portrait_key_uses_catalog_when_listed(mode: str, base: str) -> None:
    catalog = frozenset({base, f"{base}_portrait"})
    assert resolve_video_model_key(
        "omni", mode=mode, is_portrait=True, duration_seconds=4, extra_valid_keys=catalog
    ) == (f"{base}_portrait", False)
    assert resolve_video_model_key(
        "omni", mode=mode, is_portrait=False, duration_seconds=4, extra_valid_keys=catalog
    ) == (base, False)


@pytest.mark.parametrize(("mode", "base"), sorted(_OMNI_BASES.items()))
def test_omni_portrait_key_falls_back_when_catalog_lacks_it(mode: str, base: str) -> None:
    catalog = frozenset({base})
    assert resolve_video_model_key(
        "omni", mode=mode, is_portrait=True, duration_seconds=4, extra_valid_keys=catalog
    ) == (base, False)


def test_raw_omni_portrait_key_accepted_offline() -> None:
    assert resolve_video_model_key("macaron_cookie_t2v_4s_portrait", mode="TEXT", is_portrait=True) == (
        "macaron_cookie_t2v_4s_portrait",
        False,
    )


def test_build_generate_video_req_threads_catalog_into_omni_portrait_key() -> None:
    _, req, _ = build_generate_video_req(
        "p1",
        "vertical puppy",
        model="omni",
        duration_seconds=4,
        aspect_ratio="9:16",
        valid_video_model_keys=frozenset({"macaron_cookie_t2v_4s", "macaron_cookie_t2v_4s_portrait"}),
    )
    assert req[0][0][1] == "macaron_cookie_t2v_4s_portrait"
    assert req[0][0][2] == resolve_video_aspect("9:16")[0]

    _, req_fb, _ = build_generate_video_req(
        "p1",
        "vertical puppy",
        model="omni",
        duration_seconds=4,
        aspect_ratio="9:16",
        valid_video_model_keys=frozenset({"macaron_cookie_t2v_4s"}),
    )
    assert req_fb[0][0][1] == "macaron_cookie_t2v_4s"
    assert req_fb[0][0][2] == resolve_video_aspect("9:16")[0]


# 需用真实 Flow 网页抓包 YhhmEf 请求核对后，把样本写进这里并去掉 xfail
# 格式：{"9:16": <抓包中比例字段的值>, "16:9": <...>, "1:1": <...>}
CAPTURED_VIDEO_ASPECT_SAMPLES: dict[str, int] = {}


@pytest.mark.xfail(strict=False, reason="video aspect enum (1:1=0, 9:16=1, 16:9=2) not yet verified by capture")
def test_video_aspect_enum_matches_capture() -> None:
    # 需用真实 Flow 网页抓包 YhhmEf 请求核对后，把样本写进这里并去掉 xfail
    if not CAPTURED_VIDEO_ASPECT_SAMPLES:
        pytest.fail("No captured YhhmEf samples recorded yet.")
    for ratio, wire_value in CAPTURED_VIDEO_ASPECT_SAMPLES.items():
        assert resolve_video_aspect(ratio)[0] == wire_value


def _load_hardening_module() -> ModuleType:
    for name in ("test_production_hardening", "tests.test_production_hardening"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    pytest.skip("tests/test_production_hardening.py helpers not importable in this pytest import mode")
    raise AssertionError("unreachable")


def test_mcp_generate_video_rejects_key_reuse_with_new_aspect(monkeypatch, tmp_path: Path) -> None:
    hardening = _load_hardening_module()
    from fastmcp.exceptions import ToolError

    from gflow.mcp._filelink import FileLinkSigner, FileTransferConfig
    from gflow.mcp._jobs import JobStore
    from gflow.mcp.server import create_server

    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))

    async def main() -> None:
        mock = hardening.HardenedMockClient()
        ft = FileTransferConfig(
            signer=FileLinkSigner.from_persistent_store(tmp_path / "gflow_home" / "secret.key"),
            base_url="https://flow-host.example.ts.net:8443",
        )
        server = create_server(
            client_factory=lambda: mock,  # type: ignore[arg-type]
            file_transfer=ft,
            job_store=JobStore(tmp_path / "gflow_home" / "jobs.json"),
        )
        base_args = {
            "prompt": "Marley vertical shot",
            "model": "omni",
            "duration_seconds": 4,
            "idempotency_key": "marley-clip-1",
            "wait": False,
        }
        first = hardening._extract_json(
            await server.call_tool("flow_generate_video", {**base_args, "aspect_ratio": "16:9"})
        )
        again = hardening._extract_json(
            await server.call_tool("flow_generate_video", {**base_args, "aspect_ratio": "16:9"})
        )
        assert again["job_id"] == first["job_id"]
        assert again["idempotent_replay"] is True
        assert len(mock.video_submissions) == 1

        with pytest.raises(ToolError) as ei:
            await server.call_tool("flow_generate_video", {**base_args, "aspect_ratio": "9:16"})
        msg = str(ei.value)
        assert "idempotency_key" in msg or "INVALID_INPUT" in msg
        assert len(mock.video_submissions) == 1

        fresh = hardening._extract_json(
            await server.call_tool(
                "flow_generate_video",
                {**base_args, "aspect_ratio": "9:16", "idempotency_key": "marley-clip-1-portrait"},
            )
        )
        assert fresh["job_id"] != first["job_id"]
        assert fresh["idempotent_replay"] is False
        assert mock.video_submissions[-1]["aspect_ratio"] == "9:16"

    asyncio.run(main())
