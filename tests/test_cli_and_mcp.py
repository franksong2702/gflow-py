"""Integration tests for CLI commands, MCP boundary cleanliness, MCP tools, and /files/* routes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
from click.testing import CliRunner

from gflow.cli.main import cli
from gflow.mcp._filelink import FileLinkSigner, FileTransferConfig
from gflow.mcp.server import create_server
from gflow.types import AccountCredits, MediaItem, ModelVariant, Project, ProjectContents, Scene, UserAccount


class FakeRpc:
    async def download_bytes(self, url: str) -> tuple[bytes, str]:
        return b"FAKE_MP4_BINARY_DATA", "video/mp4"


class FakeFlowClient:
    def __init__(self) -> None:
        self._rpc = FakeRpc()
        self.deleted_ids: list[str] = []

    async def __aenter__(self) -> FakeFlowClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def close(self) -> None:
        return None

    async def whoami(self) -> UserAccount:
        return UserAccount(user_id="u1", email="demo@example.com", display_name="Demo User")

    async def get_credits(self) -> AccountCredits:
        return AccountCredits(total_credits=24000, subscription_credits=24000)

    async def list_models(self, kind: str | None = None) -> list[ModelVariant]:
        return [ModelVariant(key="veo_3_1_t2v_fast_ultra", family_id="VEO", family_title="Veo 3.1", kind="video")]

    async def list_projects(self, limit: int = 30) -> list[Project]:
        return [Project(id="11111111-2222-3333-4444-555555555555", title="Demo Project")]

    async def resolve_project_id(self, pid: str | None = None) -> str:
        return pid or "11111111-2222-3333-4444-555555555555"

    async def get_project(self, pid: str) -> Project:
        return Project(id="11111111-2222-3333-4444-555555555555", title="Demo Project")

    async def delete_project(self, pid: str) -> bool:
        self.deleted_ids.append(pid)
        return True

    async def get_project_contents(self, pid: str | None = None) -> ProjectContents:
        return ProjectContents(
            project_id="11111111-2222-3333-4444-555555555555",
            media=[
                MediaItem(
                    id="media-vid-001",
                    project_id="11111111-2222-3333-4444-555555555555",
                    media_type="video",
                    status="completed",
                    video_url="https://storage.googleapis.com/fake/vid.mp4",
                )
            ],
        )

    async def get_media(self, media_id: str) -> MediaItem:
        return MediaItem(
            id=media_id,
            project_id="11111111-2222-3333-4444-555555555555",
            media_type="video",
            status="completed",
            mime_type="video/mp4",
            video_url="https://storage.googleapis.com/fake/vid.mp4",
        )

    async def upload_image(
        self,
        project_id: str | None,
        source: str | Path | bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
        is_hidden: bool = False,
    ) -> MediaItem:
        return MediaItem(
            id="uploaded-img-999",
            project_id=project_id or "11111111-2222-3333-4444-555555555555",
            media_type="image",
            status="completed",
            mime_type=mime_type or "image/png",
            width=1920,
            height=1080,
            image_url="https://storage.googleapis.com/fake/img.png",
        )

    async def generate_video(self, project_id: str | None, prompt: str, **kwargs: Any) -> list[MediaItem]:
        return [
            MediaItem(
                id="gen-vid-777",
                project_id=project_id or "11111111-2222-3333-4444-555555555555",
                media_type="video",
                status="completed",
                prompt=prompt,
                video_url="https://storage.googleapis.com/fake/gen.mp4",
            )
        ]

    async def poll_media(self, media_ids: list[str], **kwargs: Any) -> list[MediaItem]:
        return [await self.get_media(mid) for mid in media_ids]

    async def list_scenes(self, project_id: str | None = None) -> list[Scene]:
        return [Scene(id="scene-001", project_id="11111111-2222-3333-4444-555555555555", title="Main Cut")]


def test_mcp_modules_do_not_import_cli_or_click() -> None:
    mcp_mods = [m for name, m in sys.modules.items() if name.startswith("gflow.mcp") and m is not None]
    assert mcp_mods
    for mod in mcp_mods:
        src = Path(mod.__file__).read_text(encoding="utf-8") if getattr(mod, "__file__", None) else ""
        assert "import click" not in src
        assert "import rich" not in src


async def test_mcp_tools_and_signed_file_routes_end_to_end(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "gflow_home"))
    fake = FakeFlowClient()
    ft = FileTransferConfig(
        signer=FileLinkSigner(b"abcdef0123456789abcdef0123456789"),
        base_url="https://flow-host.example.ts.net:8443",
    )
    server = create_server(client_factory=lambda: fake, file_transfer=ft)  # type: ignore[arg-type]

    tools = await server.list_tools()
    tool_names = {t.name for t in tools}
    assert "flow_account_usage" in tool_names
    assert "flow_generate_video" in tool_names
    assert "flow_generate_image" in tool_names
    assert "flow_create_upload_link" in tool_names
    assert "flow_scene_export_mp4" in tool_names
    assert len(tool_names) >= 22

    # Safety confirmation gate on flow_project_delete
    del_preview = await server.call_tool("flow_project_delete", {"project_id": "11111111-2222-3333-4444-555555555555"})
    assert "needs_confirmation" in str(del_preview)
    assert fake.deleted_ids == []

    del_confirmed = await server.call_tool(
        "flow_project_delete",
        {"project_id": "11111111-2222-3333-4444-555555555555", "confirm": True},
    )
    assert "deleted" in str(del_confirmed)
    assert len(fake.deleted_ids) == 1

    # Signed HTTP /files/dl and /files/ul routes via Starlette ASGI app
    asgi_app = server.http_app()
    dl_url = ft.download_url_for_media("media-vid-001")
    dl_path = "/" + dl_url.split(":8443/", 1)[1]

    ul_meta = ft.create_upload_link("11111111-2222-3333-4444-555555555555", label="Hero Frame")
    ul_path = "/" + ul_meta["upload_url"].split(":8443/", 1)[1]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="https://flow-host.example.ts.net:8443",
    ) as client:
        # 1. Signed media download streams video/mp4
        r_dl = await client.get(dl_path)
        assert r_dl.status_code == 200
        assert r_dl.headers["content-type"].startswith("video/mp4")
        assert r_dl.content == b"FAKE_MP4_BINARY_DATA"

        # 2. Signed browser upload page renders HTML picker
        r_ul_page = await client.get(ul_path)
        assert r_ul_page.status_code == 200
        assert "Upload to Google Flow" in r_ul_page.text

        # 3. Raw POST uploads image, commits single-use JTI, and exposes media_id via flow_check_upload
        r_ul_post = await client.post(
            f"{ul_path}?filename=hero.png",
            content=b"\x89PNG\r\n\x1a\nFAKEPNG",
            headers={"Content-Type": "image/png"},
        )
        assert r_ul_post.status_code == 200
        assert r_ul_post.json()["media_id"] == "uploaded-img-999"

        # 4. Replay of single-use upload link is rejected with 403
        r_ul_replay = await client.post(
            f"{ul_path}?filename=hero.png",
            content=b"\x89PNG\r\n\x1a\nFAKEPNG",
            headers={"Content-Type": "image/png"},
        )
        assert r_ul_replay.status_code == 403

    status = ft.jti_store.status(ul_meta["upload_id"])
    assert status["status"] == "completed"
    assert status["media_id"] == "uploaded-img-999"


def test_cli_help_and_skill_show(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GFLOW_HOME", str(tmp_path / "cli_home"))
    runner = CliRunner()
    res_help = runner.invoke(cli, ["--help"])
    assert res_help.exit_code == 0
    assert "generate" in res_help.output
    assert "scene" in res_help.output

    res_skill = runner.invoke(cli, ["skill", "show"])
    assert res_skill.exit_code == 0
    assert "Google Flow" in res_skill.output
