"""Security tests for the restricted (public HTTP) transport: output_path confinement, file_path, profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import gflow.mcp.server as server_mod
from gflow.exceptions import ValidationError
from gflow.mcp import _fileroutes
from gflow.mcp._pathguard import (
    OUTSIDE_ROOT_MESSAGE,
    SYMLINK_TARGET_MESSAGE,
    resolve_confined_output,
)
from gflow.mcp.server import create_server
from gflow.paths import get_active_profile
from gflow.types import AccountCredits, MediaItem, Project, UserAccount

VALID_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
FAKE_VIDEO_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + (b"V" * 4096)

FILE_PATH_DISABLED_MSG = (
    "file_path is disabled on the remote HTTP transport; use image_base64 or flow_create_upload_link."
)
PROFILE_SWITCH_MSG = "Switching profile is disabled on the remote HTTP transport"


class RestrictedMockClient:
    """Minimal deterministic mock FlowClient for restricted-transport security tests."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, int]] = []
        self.get_media_calls: int = 0
        self.download_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._rpc = self

    async def __aenter__(self) -> RestrictedMockClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def whoami(self) -> UserAccount:
        return UserAccount(user_id="u1", email="agent@example.com", display_name="Remote Agent")

    async def get_credits(self) -> AccountCredits:
        return AccountCredits(total_credits=1000, subscription_credits=1000)

    async def resolve_project_id(self, pid: str | None = None) -> str:
        return pid or "proj-1111"

    async def get_project(self, pid: str) -> Project:
        return Project(id=pid, title="Restricted Project")

    async def upload_image(
        self,
        project_id: str | None,
        source: str | Path | bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        is_hidden: bool = False,
    ) -> MediaItem:
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

    async def get_media(self, media_id: str) -> MediaItem:
        self.get_media_calls += 1
        return MediaItem(
            id=media_id,
            project_id="proj-1111",
            media_type="video",
            status="completed",
            mime_type="video/mp4",
            video_url="https://storage.googleapis.com/fake/vid.mp4",
        )

    async def download_bytes(self, url: str) -> tuple[bytes, str]:
        return FAKE_VIDEO_MP4, "video/mp4"

    async def download_media(self, *args: Any, **kwargs: Any) -> Path | None:
        self.download_calls.append((args, kwargs))
        for value in list(args) + list(kwargs.values()):
            if isinstance(value, Path) or (isinstance(value, str) and value.endswith(".mp4")):
                target = Path(value)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(FAKE_VIDEO_MP4)
                return target
        return None


def _symlink_or_skip(link: Path, target: Path, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")


async def _call(server: Any, name: str, args: dict[str, Any]) -> str:
    """Invoke a tool through the in-process fastmcp Client and return all textual output (errors included)."""
    async with Client(server) as client:
        try:
            result = await client.call_tool(name, args)
        except ToolError as exc:
            return str(exc)
    parts: list[str] = []
    content = getattr(result, "content", result)
    for block in content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        parts.append(json.dumps(structured, default=str))
    return "\n".join(parts)


@pytest.fixture
def exports_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "exports"
    root.mkdir()
    monkeypatch.setattr(_fileroutes, "get_exports_dir", lambda: root)
    return root


@pytest.fixture
def outside_dir(tmp_path: Path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    return d


@pytest.fixture
def mock() -> RestrictedMockClient:
    return RestrictedMockClient()


@pytest.fixture
def restricted_server(mock: RestrictedMockClient, exports_root: Path) -> Any:
    return create_server(restricted=True, client_factory=lambda *a, **k: mock)


@pytest.fixture
def open_server(mock: RestrictedMockClient, exports_root: Path) -> Any:
    return create_server(client_factory=lambda *a, **k: mock)


# ── Unit tests: _pathguard.resolve_confined_output ───────────────────────────


def test_pathguard_relative_inside_root_allowed(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    assert resolve_confined_output("clips/out.mp4", root) == root.resolve() / "clips" / "out.mp4"


def test_pathguard_absolute_inside_root_allowed(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    target = root / "a.mp4"
    assert resolve_confined_output(str(target), root) == root.resolve() / "a.mp4"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "a\x00b.mp4", "../escape.mp4", "clips/../../escape.mp4", ".", "clips/.."],
)
def test_pathguard_rejects_bad_inputs(tmp_path: Path, bad: str) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    with pytest.raises(ValidationError) as excinfo:
        resolve_confined_output(bad, root)
    assert str(tmp_path) not in str(excinfo.value)


def test_pathguard_rejects_absolute_outside_without_leaking_path(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    with pytest.raises(ValidationError) as excinfo:
        resolve_confined_output(str(tmp_path / "outside" / "x.mp4"), root)
    assert OUTSIDE_ROOT_MESSAGE in str(excinfo.value)
    assert str(tmp_path) not in str(excinfo.value)
    with pytest.raises(ValidationError):
        resolve_confined_output("/etc/passwd", root)


def test_pathguard_rejects_symlinked_directory_escape(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(root / "link", outside, is_dir=True)
    with pytest.raises(ValidationError) as excinfo:
        resolve_confined_output("link/out.mp4", root)
    assert OUTSIDE_ROOT_MESSAGE in str(excinfo.value)


def test_pathguard_rejects_symlink_target_even_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    real = root / "real.mp4"
    real.write_bytes(b"x")
    _symlink_or_skip(root / "alias.mp4", real)
    with pytest.raises(ValidationError) as excinfo:
        resolve_confined_output("alias.mp4", root)
    assert SYMLINK_TARGET_MESSAGE in str(excinfo.value)


# ── Tool-level tests: output_path confinement (item 1) ───────────────────────


async def test_get_media_absolute_output_path_escape_rejected(
    restricted_server: Any, mock: RestrictedMockClient, outside_dir: Path, tmp_path: Path
) -> None:
    target = outside_dir / "x.mp4"
    text = await _call(
        restricted_server, "flow_get_media", {"media_id": "vid-001", "output_path": str(target)}
    )
    assert OUTSIDE_ROOT_MESSAGE in text
    assert str(tmp_path) not in text
    assert not target.exists()
    assert mock.get_media_calls == 0
    assert mock.download_calls == []


async def test_get_media_dotdot_output_path_escape_rejected(
    restricted_server: Any, mock: RestrictedMockClient, outside_dir: Path, tmp_path: Path
) -> None:
    text = await _call(
        restricted_server,
        "flow_get_media",
        {"media_id": "vid-001", "output_path": "../outside/x.mp4"},
    )
    assert OUTSIDE_ROOT_MESSAGE in text
    assert str(tmp_path) not in text
    assert not (outside_dir / "x.mp4").exists()
    assert mock.get_media_calls == 0
    assert mock.download_calls == []


async def test_get_media_symlink_directory_escape_rejected(
    restricted_server: Any,
    mock: RestrictedMockClient,
    exports_root: Path,
    outside_dir: Path,
    tmp_path: Path,
) -> None:
    _symlink_or_skip(exports_root / "link", outside_dir, is_dir=True)
    text = await _call(
        restricted_server,
        "flow_get_media",
        {"media_id": "vid-001", "output_path": "link/out.mp4"},
    )
    assert OUTSIDE_ROOT_MESSAGE in text
    assert str(tmp_path) not in text
    assert not (outside_dir / "out.mp4").exists()
    assert mock.download_calls == []


async def test_get_media_symlink_target_escape_rejected(
    restricted_server: Any,
    mock: RestrictedMockClient,
    exports_root: Path,
    outside_dir: Path,
    tmp_path: Path,
) -> None:
    victim = outside_dir / "victim.mp4"
    victim.write_bytes(b"original")
    _symlink_or_skip(exports_root / "alias.mp4", victim)
    text = await _call(
        restricted_server,
        "flow_get_media",
        {"media_id": "vid-001", "output_path": "alias.mp4"},
    )
    assert SYMLINK_TARGET_MESSAGE in text
    assert str(tmp_path) not in text
    assert victim.read_bytes() == b"original"
    assert mock.download_calls == []


async def test_get_media_relative_output_path_inside_root_allowed(
    restricted_server: Any,
    exports_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_paths: list[Path] = []
    real_guard = server_mod.resolve_confined_output

    def _spy(output_path: str, root: Path) -> Path:
        result = real_guard(output_path, root)
        resolved_paths.append(result)
        return result

    monkeypatch.setattr(server_mod, "resolve_confined_output", _spy)
    text = await _call(
        restricted_server,
        "flow_get_media",
        {"media_id": "vid-001", "output_path": "clips/out.mp4"},
    )
    assert OUTSIDE_ROOT_MESSAGE not in text
    assert SYMLINK_TARGET_MESSAGE not in text
    assert resolved_paths == [exports_root.resolve() / "clips" / "out.mp4"]


async def test_scene_export_absolute_output_path_escape_rejected(
    restricted_server: Any, outside_dir: Path, tmp_path: Path
) -> None:
    target = outside_dir / "scene.mp4"
    text = await _call(
        restricted_server,
        "flow_scene_export_mp4",
        {"scene_id": "scene-1", "output_path": str(target)},
    )
    assert OUTSIDE_ROOT_MESSAGE in text
    assert str(tmp_path) not in text
    assert not target.exists()


async def test_scene_export_dotdot_output_path_escape_rejected(
    restricted_server: Any, outside_dir: Path, tmp_path: Path
) -> None:
    text = await _call(
        restricted_server,
        "flow_scene_export_mp4",
        {"scene_id": "scene-1", "output_path": "../outside/scene.mp4"},
    )
    assert OUTSIDE_ROOT_MESSAGE in text
    assert str(tmp_path) not in text
    assert not (outside_dir / "scene.mp4").exists()


async def test_unrestricted_server_does_not_confine_output_path(
    open_server: Any, outside_dir: Path
) -> None:
    text = await _call(
        open_server,
        "flow_get_media",
        {"media_id": "vid-001", "output_path": str(outside_dir / "local.mp4")},
    )
    assert OUTSIDE_ROOT_MESSAGE not in text


# ── Tool-level tests: file_path disabled (item 3) ────────────────────────────


async def test_upload_image_file_path_rejected_identically_for_existing_and_missing(
    restricted_server: Any, mock: RestrictedMockClient, tmp_path: Path
) -> None:
    existing = tmp_path / "real.png"
    existing.write_bytes(VALID_PNG)
    missing = tmp_path / "does-not-exist.png"

    text_existing = await _call(restricted_server, "flow_upload_image", {"file_path": str(existing)})
    text_missing = await _call(restricted_server, "flow_upload_image", {"file_path": str(missing)})

    for text in (text_existing, text_missing):
        assert FILE_PATH_DISABLED_MSG in text
        assert "does not exist" not in text
        assert str(tmp_path) not in text
    assert mock.uploads == []


async def test_upload_image_file_path_sensitive_file_rejected(
    restricted_server: Any, mock: RestrictedMockClient
) -> None:
    text = await _call(restricted_server, "flow_upload_image", {"file_path": "/etc/passwd"})
    assert FILE_PATH_DISABLED_MSG in text
    assert "root:" not in text
    assert mock.uploads == []


async def test_unrestricted_server_still_accepts_file_path(
    open_server: Any, mock: RestrictedMockClient, tmp_path: Path
) -> None:
    existing = tmp_path / "real.png"
    existing.write_bytes(VALID_PNG)
    text = await _call(open_server, "flow_upload_image", {"file_path": str(existing)})
    assert FILE_PATH_DISABLED_MSG not in text
    assert len(mock.uploads) == 1


# ── Tool-level tests: profile switching disabled (item 4) ────────────────────


@pytest.fixture
def forbid_other_profile(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("must not open another profile's storage in restricted mode")

    monkeypatch.setattr(server_mod.FlowClient, "from_storage", _boom)
    return calls


async def test_account_usage_profile_switch_rejected(
    restricted_server: Any, forbid_other_profile: list[Any]
) -> None:
    text = await _call(restricted_server, "flow_account_usage", {"profile": "expired-test"})
    assert PROFILE_SWITCH_MSG in text
    assert forbid_other_profile == []


async def test_project_list_profile_switch_rejected(
    restricted_server: Any, forbid_other_profile: list[Any]
) -> None:
    text = await _call(
        restricted_server, "flow_project_list", {"limit": 5, "profile": "someone-else"}
    )
    assert PROFILE_SWITCH_MSG in text
    assert forbid_other_profile == []


async def test_account_usage_same_profile_not_rejected(
    restricted_server: Any, forbid_other_profile: list[Any]
) -> None:
    active = get_active_profile(None)
    text = await _call(restricted_server, "flow_account_usage", {"profile": active})
    assert PROFILE_SWITCH_MSG not in text
    assert forbid_other_profile == []
