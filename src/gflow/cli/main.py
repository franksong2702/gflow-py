"""`gflow` CLI — notebooklm-py-style command-line interface for Google Flow."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..auth import (
    doctor_check,
    find_notebooklm_storage_state,
    import_storage_state,
    login_via_browser,
)
from ..client import FlowClient
from ..exceptions import GFlowError
from ..paths import (
    get_active_profile,
    list_profiles,
    load_context,
    save_context,
    set_active_profile,
)
from ..skill import (
    get_skill_markdown,
    install_mcp_config,
    install_skill,
    skill_status,
    uninstall_skill,
)

console = Console()
err_console = Console(stderr=True)


@dataclass
class CliState:
    profile: str | None
    storage: str | None
    as_json: bool

    def make_client(self) -> FlowClient:
        return FlowClient.from_storage(profile=self.profile, storage_path=self.storage)


def _emit_json(payload: Any) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except GFlowError as exc:
        err_console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(2)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="gflow")
@click.option("-p", "--profile", default=None, help="Auth profile name (default: active or 'default').")
@click.option("--storage", default=None, help="Explicit path to Playwright storage_state.json.")
@click.option("--json", "as_json", is_flag=True, help="Emit structured JSON output for agents/scripts.")
@click.pass_context
def cli(ctx: click.Context, profile: str | None, storage: str | None, as_json: bool) -> None:
    """Google Flow CLI — AI video, image & Scene Builder studio for humans and agents."""
    ctx.obj = CliState(profile=profile, storage=storage, as_json=as_json)


# ── Session & Auth Commands ──────────────────────────────────────────────────


@cli.command("login")
@click.option(
    "--browser",
    "browser_channel",
    type=click.Choice(["chromium", "chrome", "msedge"]),
    default="chromium",
    show_default=True,
    help="Browser engine to launch for Google sign-in.",
)
@click.option("--browser-timeout", default=300, show_default=True, help="Seconds to wait for login.")
@click.option("--fresh", is_flag=True, help="Start with a clean browser profile directory.")
@click.option(
    "--from-notebooklm",
    is_flag=True,
    help="Reuse existing Google session from ~/.notebooklm without opening a browser.",
)
@click.option(
    "--import-storage",
    default=None,
    type=click.Path(exists=True),
    help="Import an existing Playwright storage_state.json file.",
)
@click.pass_obj
def cmd_login(
    state: CliState,
    browser_channel: str,
    browser_timeout: int,
    fresh: bool,
    from_notebooklm: bool,
    import_storage: str | None,
) -> None:
    """Authenticate with Google Flow via browser or existing Google storage_state."""
    prof = get_active_profile(state.profile)
    if from_notebooklm or import_storage:
        src = Path(import_storage) if import_storage else find_notebooklm_storage_state()
        saved = import_storage_state(src, profile=prof)
    else:
        console.print(f"[cyan]Profile:[/cyan] {prof}")
        console.print(f"Opening {browser_channel} for Google Flow login (up to {browser_timeout}s)...")
        saved = _run(
            login_via_browser(
                profile=prof,
                browser_channel=browser_channel,
                timeout_seconds=browser_timeout,
                fresh=fresh,
                storage_override=Path(state.storage) if state.storage else None,
            )
        )

    async def _verify() -> dict[str, Any]:
        async with FlowClient.from_storage(profile=prof, storage_path=saved) as client:
            acct = await client.whoami()
            credits = await client.get_credits()
            return {"account": acct.to_dict(), "credits": credits.to_dict(), "storage_path": str(saved)}

    info = _run(_verify())
    if state.as_json:
        _emit_json(info)
        return
    email = info["account"].get("email") or info["account"].get("user_id") or "authenticated"
    total_c = info["credits"]["total_credits"]
    console.print(f"[bold green]Authenticated![/bold green] Account: [bold]{email}[/bold] | Credits: [bold]{total_c}[/bold]")
    console.print(f"Saved credentials to: {saved}")


@cli.command("doctor")
@click.pass_obj
def cmd_doctor(state: CliState) -> None:
    """Diagnose local profile, cookies, Playwright browser, and live Flow connectivity."""
    report = doctor_check(state.profile, explicit_storage=state.storage)
    if report["cookies_valid"]:
        async def _live() -> dict[str, Any]:
            async with state.make_client() as client:
                acct = await client.whoami()
                cred = await client.get_credits()
                return {"live_auth_ok": True, "email": acct.email, "credits": cred.total_credits}

        try:
            report.update(asyncio.run(_live()))
        except Exception as exc:
            report["live_auth_ok"] = False
            report["live_error"] = str(exc)

    if state.as_json:
        _emit_json(report)
        return
    for k, v in report.items():
        console.print(f"[bold]{k}:[/bold] {v}")


@cli.command("status")
@click.pass_obj
def cmd_status(state: CliState) -> None:
    """Show current profile and active project context."""
    prof = get_active_profile(state.profile)
    ctx = load_context(prof)
    payload = {"profile": prof, **ctx}
    if state.as_json:
        _emit_json(payload)
        return
    console.print(f"[bold]Active Profile:[/bold] {prof}")
    if ctx.get("project_id"):
        console.print(f"[bold]Active Project:[/bold] {ctx.get('project_title', 'Untitled')} ({ctx['project_id']})")
    else:
        console.print("[dim]No active project set. Run 'gflow use <project_id>' or 'gflow list'.[/dim]")


@cli.command("use")
@click.argument("project_id")
@click.pass_obj
def cmd_use(state: CliState, project_id: str) -> None:
    """Set the active Google Flow project (accepts full UUID, prefix, or exact title)."""
    prof = get_active_profile(state.profile)

    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            proj = await client.get_project(project_id)
            ctx = load_context(prof)
            ctx["project_id"] = proj.id
            ctx["project_title"] = proj.title
            save_context(ctx, prof)
            return proj.to_dict()

    proj_dict = _run(_do())
    if state.as_json:
        _emit_json(proj_dict)
        return
    console.print(f"[green]Active project set to:[/green] [bold]{proj_dict['title']}[/bold] ({proj_dict['id']})")


@cli.command("clear")
@click.pass_obj
def cmd_clear(state: CliState) -> None:
    """Clear the active project context."""
    prof = get_active_profile(state.profile)
    save_context({}, prof)
    if state.as_json:
        _emit_json({"cleared": True, "profile": prof})
    else:
        console.print("Cleared active project context.")


@cli.command("usage")
@click.pass_obj
def cmd_usage(state: CliState) -> None:
    """Show live Google Flow AI credits and subscription tier."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            cred = await client.get_credits()
            return cred.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    table = Table(title="Google Flow AI Credits")
    table.add_column("Total Credits", justify="right", style="bold green")
    table.add_column("Subscription", justify="right")
    table.add_column("Top-up", justify="right")
    table.add_column("Service Tier", justify="center")
    table.add_column("Renews At (UTC)")
    table.add_row(
        str(data["total_credits"]),
        str(data["subscription_credits"]),
        str(data["topup_credits"]),
        str(data["service_tier"] or "-"),
        str(data["renews_at"] or "-"),
    )
    console.print(table)


@cli.command("models")
@click.option("--kind", type=click.Choice(["video", "image", "audio"]), default=None, help="Filter by model kind.")
@click.pass_obj
def cmd_models(state: CliState, kind: str | None) -> None:
    """List live video, image, and audio models available to your account."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            items = await client.list_models(kind=kind)
            return [m.to_dict() for m in items]

    models = _run(_do())
    if state.as_json:
        _emit_json(models)
        return
    table = Table(title=f"Google Flow Models ({len(models)})")
    table.add_column("Kind", style="cyan")
    table.add_column("Family", style="bold")
    table.add_column("Model Key", style="green")
    table.add_column("Duration", justify="right")
    for m in models:
        dur = f"{m['duration_seconds']}s" if m["duration_seconds"] else "-"
        table.add_row(m["kind"], m["family_title"], m["key"], dur)
    console.print(table)


# ── Project Commands ─────────────────────────────────────────────────────────


@cli.command("list")
@click.option("-n", "--limit", default=25, show_default=True, help="Maximum projects to return.")
@click.pass_obj
def cmd_list(state: CliState, limit: int) -> None:
    """List Google Flow projects."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            projs = await client.list_projects(limit=limit)
            return [p.to_dict() for p in projs]

    items = _run(_do())
    if state.as_json:
        _emit_json(items)
        return
    table = Table(title="Google Flow Projects")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="bold")
    table.add_column("Created (UTC)")
    for p in items:
        created = (p["created_at"] or "")[:19].replace("T", " ")
        table.add_row(p["id"], p["title"], created)
    console.print(table)


@cli.command("create")
@click.argument("title")
@click.option("--use/--no-use", default=True, show_default=True, help="Set newly created project as active.")
@click.pass_obj
def cmd_create(state: CliState, title: str, use: bool) -> None:
    """Create a new Google Flow project."""
    prof = get_active_profile(state.profile)

    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            proj = await client.create_project(title)
            if use:
                save_context({"project_id": proj.id, "project_title": proj.title}, prof)
            return proj.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[bold green]Created project:[/bold green] {data['title']} ({data['id']})")


@cli.command("rename")
@click.argument("new_title")
@click.option("-P", "--project", default=None, help="Target project ID (default: active project).")
@click.pass_obj
def cmd_rename(state: CliState, new_title: str, project: str | None) -> None:
    """Rename a Google Flow project."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            proj = await client.rename_project(project or "", new_title)
            return proj.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[green]Renamed project to:[/green] {data['title']} ({data['id']})")


@cli.command("delete")
@click.argument("project_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_obj
def cmd_delete(state: CliState, project_id: str, yes: bool) -> None:
    """Delete a Google Flow project."""
    if not yes:
        click.confirm(f"Delete Google Flow project {project_id}?", abort=True)

    async def _do() -> str:
        async with state.make_client() as client:
            pid = await client.resolve_project_id(project_id)
            await client.delete_project(pid)
            return pid

    deleted_id = _run(_do())
    if state.as_json:
        _emit_json({"deleted": True, "project_id": deleted_id})
    else:
        console.print(f"[yellow]Deleted project:[/yellow] {deleted_id}")


# ── Media, Upload, Generation & Download Commands ────────────────────────────


@cli.command("media")
@click.option("-P", "--project", default=None, help="Project ID (default: active project).")
@click.option("--type", "media_type", type=click.Choice(["all", "video", "image"]), default="all")
@click.pass_obj
def cmd_media(state: CliState, project: str | None, media_type: str) -> None:
    """List generated and uploaded media assets in a project."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            contents = await client.get_project_contents(project)
            items = contents.media
            if media_type != "all":
                items = [m for m in items if m.media_type == media_type]
            return {"project_id": contents.project_id, "media": [m.to_dict() for m in items]}

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    table = Table(title=f"Media Assets in Project {data['project_id'][:8]} ({len(data['media'])})")
    table.add_column("Media ID", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Dur/Dim", justify="right")
    table.add_column("Prompt")
    for m in data["media"]:
        spec = (
            f"{m['duration_seconds']}s"
            if m["duration_seconds"]
            else (f"{m['width']}x{m['height']}" if m["width"] else "-")
        )
        prompt = (m["prompt"] or "-")[:48]
        table.add_row(m["id"], m["media_type"], m["status"], m["model_key"] or "-", spec, prompt)
    console.print(table)


@cli.command("upload")
@click.argument("image_path", type=click.Path(exists=True))
@click.option("-P", "--project", default=None, help="Project ID (default: active project).")
@click.pass_obj
def cmd_upload(state: CliState, image_path: str, project: str | None) -> None:
    """Upload a local reference/frame image into a Google Flow project."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            item = await client.upload_image(project, image_path)
            return item.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[bold green]Uploaded image![/bold green] Media ID: [cyan]{data['id']}[/cyan]")


@cli.command("frame-save")
@click.argument("video_media_id")
@click.option("--at", "timestamp", default=0.0, show_default=True, type=float, help="Timestamp in seconds.")
@click.option("-P", "--project", default=None, help="Project ID (default: active project).")
@click.pass_obj
def cmd_frame_save(state: CliState, video_media_id: str, timestamp: float, project: str | None) -> None:
    """Extract a frame from an existing Flow video clip as a reusable image asset."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            item = await client.save_video_frame(project, video_media_id, timestamp)
            return item.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[bold green]Saved frame at {timestamp}s![/bold green] Image Media ID: [cyan]{data['id']}[/cyan]")


@cli.group("generate")
def grp_generate() -> None:
    """Generate Nano Banana images or Veo 3.1 / Omni 1.2 videos."""


@grp_generate.command("image")
@click.argument("prompt")
@click.option("-P", "--project", default=None, help="Project ID (default: active project).")
@click.option("--model", default="pro", show_default=True, help="Model: pro (Nano Banana Pro), 2.1 (Beluga), lite, or raw key.")
@click.option("--aspect", default="16:9", show_default=True, help="Aspect ratio: 16:9, 9:16, 1:1, 4:3, 3:4.")
@click.option("-n", "--count", default=1, show_default=True, type=int, help="Number of images (1..4).")
@click.option("--ref", "refs", multiple=True, help="Reference image media_id or local path (repeatable).")
@click.option("--base-image", default=None, help="Base image media_id or local path to transform.")
@click.option("--seed", default=None, type=int, help="Optional RNG seed.")
@click.option("--wait/--no-wait", default=True, show_default=True, help="Wait for generation to complete.")
@click.option("-d", "--download", is_flag=True, help="Automatically download generated image(s).")
@click.option("-o", "--output", default=None, help="Output file or directory when --download is set.")
@click.pass_obj
def cmd_generate_image(
    state: CliState,
    prompt: str,
    project: str | None,
    model: str,
    aspect: str,
    count: int,
    refs: tuple[str, ...],
    base_image: str | None,
    seed: int | None,
    wait: bool,
    download: bool,
    output: str | None,
) -> None:
    """Generate Nano Banana images from text and optional reference images."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            items = await client.generate_image(
                project,
                prompt,
                model=model,
                aspect_ratio=aspect,
                count=count,
                references=list(refs) if refs else None,
                base_image=base_image,
                seed=seed,
                wait=wait,
            )
            out_list: list[dict[str, Any]] = []
            for it in items:
                d = it.to_dict()
                if download and it.download_url:
                    saved = await client.download_media(it, output_path=output)
                    d["saved_path"] = str(saved)
                out_list.append(d)
            return out_list

    results = _run(_do())
    if state.as_json:
        _emit_json(results)
        return
    for r in results:
        console.print(f"[bold green]Image:[/bold green] [cyan]{r['id']}[/cyan] ({r['status']})")
        if r.get("saved_path"):
            console.print(f"  Saved: {r['saved_path']}")
        elif r.get("download_url"):
            console.print(f"  URL: {r['download_url']}")


@grp_generate.command("video")
@click.argument("prompt")
@click.option("-P", "--project", default=None, help="Project ID (default: active project).")
@click.option("--model", default="fast", show_default=True, help="Model: fast, quality, lite, omni (Omni 1.2 Flash), or raw key.")
@click.option("--aspect", default="16:9", show_default=True, help="Aspect ratio: 16:9, 9:16, 1:1.")
@click.option("--resolution", default="720p", show_default=True, help="Resolution: 720p, 1080p, 4k, 360p.")
@click.option("--duration", default=8, show_default=True, type=int, help="Target duration in seconds (e.g. 4, 6, 8).")
@click.option("-n", "--count", default=1, show_default=True, type=int, help="Number of videos (1..4).")
@click.option("--start-frame", default=None, help="First-frame image media_id or local file path.")
@click.option("--end-frame", default=None, help="Last-frame image media_id or local file path (enables Start+End Frame mode).")
@click.option("--ref", "refs", multiple=True, help="Reference ingredient image media_id or local path (repeatable).")
@click.option("--scene", "scene_id", default=None, help="Directly attach generated clip to this Scene ID.")
@click.option("--wait/--no-wait", default=True, show_default=True, help="Poll until video finishes rendering.")
@click.option("-d", "--download", is_flag=True, help="Download completed MP4 locally.")
@click.option("-o", "--output", default=None, help="Output file or folder when --download is set.")
@click.pass_obj
def cmd_generate_video(
    state: CliState,
    prompt: str,
    project: str | None,
    model: str,
    aspect: str,
    resolution: str,
    duration: int,
    count: int,
    start_frame: str | None,
    end_frame: str | None,
    refs: tuple[str, ...],
    scene_id: str | None,
    wait: bool,
    download: bool,
    output: str | None,
) -> None:
    """Generate Veo 3.1 or Omni 1.2 video (Text, Start-Frame, Start+End Frames, or Multi-Reference)."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            items = await client.generate_video(
                project,
                prompt,
                model=model,
                aspect_ratio=aspect,
                resolution=resolution,
                duration_seconds=duration,
                count=count,
                start_frame=start_frame,
                end_frame=end_frame,
                references=list(refs) if refs else None,
                scene_id=scene_id,
                wait=wait,
            )
            out_list: list[dict[str, Any]] = []
            for it in items:
                d = it.to_dict()
                if download and it.download_url:
                    saved = await client.download_media(it, output_path=output)
                    d["saved_path"] = str(saved)
                out_list.append(d)
            return out_list

    results = _run(_do())
    if state.as_json:
        _emit_json(results)
        return
    for r in results:
        console.print(f"[bold green]Video:[/bold green] [cyan]{r['id']}[/cyan] ({r['status']})")
        if r.get("saved_path"):
            console.print(f"  Saved MP4: {r['saved_path']}")
        elif r.get("download_url"):
            console.print(f"  MP4 URL: {r['download_url']}")


@cli.command("extend")
@click.argument("video_media_id")
@click.argument("prompt")
@click.option("-P", "--project", default=None, help="Project ID (default: active project).")
@click.option("--model", default="fast", show_default=True, help="Model: fast, quality, lite, or raw key.")
@click.option("--aspect", default="16:9", show_default=True, help="Aspect ratio: 16:9 or 9:16.")
@click.option("--ref", "refs", multiple=True, help="Optional reference image media_id or path.")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("-d", "--download", is_flag=True, help="Download extended MP4 locally.")
@click.option("-o", "--output", default=None)
@click.pass_obj
def cmd_extend(
    state: CliState,
    video_media_id: str,
    prompt: str,
    project: str | None,
    model: str,
    aspect: str,
    refs: tuple[str, ...],
    wait: bool,
    download: bool,
    output: str | None,
) -> None:
    """Extend an existing Flow video clip seamlessly with Veo 3.1 Video Extension."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            items = await client.extend_video(
                project,
                video_media_id,
                prompt,
                model=model,
                aspect_ratio=aspect,
                references=list(refs) if refs else None,
                wait=wait,
            )
            out_list = []
            for it in items:
                d = it.to_dict()
                if download and it.download_url:
                    saved = await client.download_media(it, output_path=output)
                    d["saved_path"] = str(saved)
                out_list.append(d)
            return out_list

    results = _run(_do())
    if state.as_json:
        _emit_json(results)
        return
    for r in results:
        console.print(f"[bold green]Extended Video:[/bold green] [cyan]{r['id']}[/cyan] ({r['status']})")
        if r.get("saved_path"):
            console.print(f"  Saved MP4: {r['saved_path']}")


@cli.command("upsample")
@click.argument("video_media_id")
@click.option("-P", "--project", default=None)
@click.option("--target", type=click.Choice(["1080p", "4k"]), default="1080p", show_default=True)
@click.option("--aspect", default="16:9", show_default=True)
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("-d", "--download", is_flag=True)
@click.option("-o", "--output", default=None)
@click.pass_obj
def cmd_upsample(
    state: CliState,
    video_media_id: str,
    project: str | None,
    target: str,
    aspect: str,
    wait: bool,
    download: bool,
    output: str | None,
) -> None:
    """Upsample a Flow video clip to 1080p or 4K."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            items = await client.upsample_video(
                project,
                video_media_id,
                target=target,
                aspect_ratio=aspect,
                wait=wait,
            )
            out_list = []
            for it in items:
                d = it.to_dict()
                if download and it.download_url:
                    saved = await client.download_media(it, output_path=output)
                    d["saved_path"] = str(saved)
                out_list.append(d)
            return out_list

    results = _run(_do())
    if state.as_json:
        _emit_json(results)
        return
    for r in results:
        console.print(f"[bold green]Upsampled Video ({target}):[/bold green] [cyan]{r['id']}[/cyan] ({r['status']})")


@cli.command("poll")
@click.argument("media_ids", nargs=-1, required=True)
@click.option("--wait/--no-wait", default=False, show_default=True, help="Keep polling until completed.")
@click.pass_obj
def cmd_poll(state: CliState, media_ids: tuple[str, ...], wait: bool) -> None:
    """Check or wait for asynchronous image/video generation status."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            items = await client.poll_media(list(media_ids), wait=wait)
            return [it.to_dict() for it in items]

    results = _run(_do())
    if state.as_json:
        _emit_json(results)
        return
    for r in results:
        console.print(f"[cyan]{r['id']}[/cyan]: status=[bold]{r['status']}[/bold] type={r['media_type']} url={r['download_url'] or '-'}")


@cli.command("download")
@click.argument("media_id")
@click.option("-o", "--output", default=None, help="Output file or directory path.")
@click.pass_obj
def cmd_download(state: CliState, media_id: str, output: str | None) -> None:
    """Download a generated image or MP4 video by Media ID."""
    async def _do() -> Path:
        async with state.make_client() as client:
            return await client.download_media(media_id, output_path=output)

    saved = _run(_do())
    if state.as_json:
        _emit_json({"media_id": media_id, "saved_path": str(saved)})
    else:
        console.print(f"[bold green]Downloaded:[/bold green] {saved}")


# ── Scene Builder Commands ───────────────────────────────────────────────────


@cli.group("scene")
def grp_scene() -> None:
    """Scene Builder — create multi-shot timelines, trim/reorder clips, and stitch MP4s."""


@grp_scene.command("list")
@click.option("-P", "--project", default=None)
@click.pass_obj
def cmd_scene_list(state: CliState, project: str | None) -> None:
    """List Scene Builder timelines in a project."""
    async def _do() -> list[dict[str, Any]]:
        async with state.make_client() as client:
            scenes = await client.list_scenes(project)
            return [s.to_dict() for s in scenes]

    scenes = _run(_do())
    if state.as_json:
        _emit_json(scenes)
        return
    table = Table(title=f"Scenes ({len(scenes)})")
    table.add_column("Scene ID", style="cyan")
    table.add_column("Title", style="bold")
    table.add_column("Aspect")
    table.add_column("Clips", justify="right")
    for s in scenes:
        table.add_row(s["id"], s["title"], s["aspect_ratio"], str(s["clip_count"]))
    console.print(table)


@grp_scene.command("get")
@click.argument("scene_id")
@click.option("-P", "--project", default=None)
@click.pass_obj
def cmd_scene_get(state: CliState, scene_id: str, project: str | None) -> None:
    """Inspect clips and trim points inside a Scene Builder timeline."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            s = await client.get_scene(project, scene_id)
            return s.to_dict()

    s = _run(_do())
    if state.as_json:
        _emit_json(s)
        return
    console.print(f"[bold]{s['title']}[/bold] ({s['id']}) | Aspect: {s['aspect_ratio']} | Clips: {s['clip_count']}")
    table = Table()
    table.add_column("#", justify="right")
    table.add_column("Workflow ID", style="cyan")
    table.add_column("Primary Media ID", style="green")
    table.add_column("Trim Start")
    table.add_column("Trim End")
    for c in s["clips"]:
        table.add_row(
            str(c["position"]),
            c["workflow_id"],
            c["primary_media_id"] or "-",
            f"{c['start_time_seconds']}s" if c["start_time_seconds"] is not None else "-",
            f"{c['end_time_seconds']}s" if c["end_time_seconds"] is not None else "-",
        )
    console.print(table)


@grp_scene.command("create")
@click.argument("title")
@click.option("-P", "--project", default=None)
@click.option("--aspect", default="16:9", show_default=True, help="Scene aspect ratio: 16:9 or 9:16.")
@click.option("--clip", "clips", multiple=True, help="Initial workflow_id or media_id to include (repeatable).")
@click.pass_obj
def cmd_scene_create(
    state: CliState,
    title: str,
    project: str | None,
    aspect: str,
    clips: tuple[str, ...],
) -> None:
    """Create a new Scene Builder timeline."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            s = await client.create_scene(project, title, aspect_ratio=aspect, clip_ids=list(clips))
            return s.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[bold green]Created Scene:[/bold green] {data['title']} ([cyan]{data['id']}[/cyan]) with {data['clip_count']} clip(s).")


@grp_scene.command("add")
@click.argument("scene_id")
@click.argument("clip_ids", nargs=-1, required=True)
@click.option("-P", "--project", default=None)
@click.option("--position", default=None, type=int, help="Insert position (default: append).")
@click.option("--start", "start_time", default=None, type=float, help="Clip start trim seconds.")
@click.option("--end", "end_time", default=None, type=float, help="Clip end trim seconds.")
@click.pass_obj
def cmd_scene_add(
    state: CliState,
    scene_id: str,
    clip_ids: tuple[str, ...],
    project: str | None,
    position: int | None,
    start_time: float | None,
    end_time: float | None,
) -> None:
    """Add one or more clips (by media_id or workflow_id) to a Scene."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            s = await client.add_scene_clips(
                project,
                scene_id,
                list(clip_ids),
                position=position,
                start_time_seconds=start_time,
                end_time_seconds=end_time,
            )
            return s.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[green]Updated Scene {data['id']}:[/green] now {data['clip_count']} clip(s).")


@grp_scene.command("reorder")
@click.argument("scene_id")
@click.option("--from-index", required=True, type=int)
@click.option("--to-index", required=True, type=int)
@click.option("-P", "--project", default=None)
@click.pass_obj
def cmd_scene_reorder(state: CliState, scene_id: str, from_index: int, to_index: int, project: str | None) -> None:
    """Move a clip from one 0-based timeline position to another."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            s = await client.reorder_scene_clip(project, scene_id, from_index, to_index)
            return s.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[green]Reordered Scene {data['id']}.[/green]")


@grp_scene.command("trim")
@click.argument("scene_id")
@click.option("--index", "clip_index", required=True, type=int, help="0-based clip position in scene.")
@click.option("--start", "start_time", default=None, type=float, help="New start trim in seconds.")
@click.option("--end", "end_time", default=None, type=float, help="New end trim in seconds.")
@click.option("-P", "--project", default=None)
@click.pass_obj
def cmd_scene_trim(
    state: CliState,
    scene_id: str,
    clip_index: int,
    start_time: float | None,
    end_time: float | None,
    project: str | None,
) -> None:
    """Trim start/end in-out points of a clip inside a Scene."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            s = await client.trim_scene_clip(
                project,
                scene_id,
                clip_index,
                start_time_seconds=start_time,
                end_time_seconds=end_time,
            )
            return s.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[green]Trimmed clip #{clip_index} in Scene {data['id']}.[/green]")


@grp_scene.command("remove")
@click.argument("scene_id")
@click.option("--index", "clip_index", required=True, type=int, help="0-based clip position to remove.")
@click.option("-P", "--project", default=None)
@click.pass_obj
def cmd_scene_remove(state: CliState, scene_id: str, clip_index: int, project: str | None) -> None:
    """Remove a clip at 0-based index from a Scene."""
    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            s = await client.remove_scene_clip(project, scene_id, clip_index)
            return s.to_dict()

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[yellow]Removed clip #{clip_index} from Scene {data['id']} ({data['clip_count']} remaining).[/yellow]")


@grp_scene.command("export")
@click.argument("scene_id")
@click.option("-P", "--project", default=None)
@click.option("-o", "--output", default=None, help="Output MP4 file path (defaults to ~/Downloads/gflow/).")
@click.pass_obj
def cmd_scene_export(state: CliState, scene_id: str, project: str | None, output: str | None) -> None:
    """Stitch and render a Scene Builder timeline into a single downloadable MP4."""
    from ..paths import get_default_output_dir

    target_out = output or str(get_default_output_dir() / f"gflow_scene_{scene_id[:8]}.mp4")

    async def _do() -> dict[str, Any]:
        async with state.make_client() as client:
            url, saved = await client.export_scene_mp4(project, scene_id, output_path=target_out)
            return {"scene_id": scene_id, "mp4_url": url, "saved_path": str(saved) if saved else None}

    data = _run(_do())
    if state.as_json:
        _emit_json(data)
        return
    console.print(f"[bold green]Exported Scene MP4![/bold green] Saved to: {data['saved_path']}")


# ── Profile, Skill & MCP Installer Commands ──────────────────────────────────


@cli.group("profile")
def grp_profile() -> None:
    """Manage multiple Google Flow authentication profiles."""


@grp_profile.command("list")
@click.pass_obj
def cmd_profile_list(state: CliState) -> None:
    active = get_active_profile(state.profile)
    profs = list_profiles()
    if state.as_json:
        _emit_json({"active": active, "profiles": profs})
        return
    for p in profs or ["default"]:
        marker = "* " if p == active else "  "
        console.print(f"{marker}{p}")


@grp_profile.command("switch")
@click.argument("name")
def cmd_profile_switch(name: str) -> None:
    set_active_profile(name)
    console.print(f"[green]Switched active profile to:[/green] {name}")


@cli.group("skill")
def grp_skill() -> None:
    """Install or manage the gflow Agent Skill for Antigravity, Claude Code, Codex, and Cursor."""


@grp_skill.command("install")
@click.option(
    "--target",
    type=click.Choice(["all", "antigravity", "claude", "codex", "cursor"]),
    default="all",
    show_default=True,
)
@click.pass_obj
def cmd_skill_install(state: CliState, target: str) -> None:
    installed = install_skill(target)
    if state.as_json:
        _emit_json(installed)
        return
    for k, path in installed.items():
        console.print(f"[green]Installed {k} skill:[/green] {path}")


@grp_skill.command("status")
@click.pass_obj
def cmd_skill_status(state: CliState) -> None:
    st = skill_status()
    if state.as_json:
        _emit_json(st)
        return
    for k, info in st.items():
        mark = "[green]installed[/green]" if info["installed"] else "[dim]not installed[/dim]"
        console.print(f"[bold]{k:12s}[/bold] {mark} ({info['path']})")


@grp_skill.command("show")
def cmd_skill_show() -> None:
    click.echo(get_skill_markdown())


@grp_skill.command("uninstall")
@click.option(
    "--target",
    type=click.Choice(["all", "antigravity", "claude", "codex", "cursor"]),
    default="all",
    show_default=True,
)
def cmd_skill_uninstall(target: str) -> None:
    removed = uninstall_skill(target)
    for k, ok in removed.items():
        if ok:
            console.print(f"[yellow]Removed {k} skill.[/yellow]")


@cli.group("mcp")
def grp_mcp() -> None:
    """Configure local stdio MCP integration for Antigravity, Claude Desktop, or Cursor."""


@grp_mcp.command("install")
@click.option(
    "--target",
    type=click.Choice(["antigravity", "claude-desktop", "cursor"]),
    default="antigravity",
    show_default=True,
)
@click.pass_obj
def cmd_mcp_install(state: CliState, target: str) -> None:
    cfg_path = install_mcp_config(target, profile=state.profile)
    if state.as_json:
        _emit_json({"target": target, "config_path": str(cfg_path)})
    else:
        console.print(f"[bold green]Registered gflow-mcp ({target}):[/bold green] {cfg_path}")
