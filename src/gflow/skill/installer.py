"""Install / inspect / remove the bundled `gflow` SKILL.md and MCP client config."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

SKILL_TARGETS: dict[str, Path] = {
    "antigravity": Path.home() / ".gemini" / "config" / "skills" / "gflow",
    "claude": Path.home() / ".claude" / "skills" / "gflow",
    "codex": Path.home() / ".codex" / "skills" / "gflow",
    "cursor": Path.home() / ".cursor" / "skills" / "gflow",
}

MCP_CONFIG_TARGETS: dict[str, tuple[Path, str]] = {
    "antigravity": (Path.home() / ".gemini" / "config" / "mcp_config.json", "mcpServers"),
    "claude-desktop": (
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "mcpServers",
    ),
    "cursor": (Path.home() / ".cursor" / "mcp.json", "mcpServers"),
}


def get_skill_markdown() -> str:
    skill_file = Path(__file__).with_name("SKILL.md")
    return skill_file.read_text(encoding="utf-8")


def _resolve_targets(target: str) -> dict[str, Path]:
    t = target.strip().lower()
    if t == "all":
        return dict(SKILL_TARGETS)
    if t not in SKILL_TARGETS:
        raise ValueError(
            f"Unknown skill target {target!r}. Choose from: {', '.join([*SKILL_TARGETS, 'all'])}"
        )
    return {t: SKILL_TARGETS[t]}


def install_skill(target: str = "all") -> dict[str, str]:
    content = get_skill_markdown()
    installed: dict[str, str] = {}
    for name, folder in _resolve_targets(target).items():
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / "SKILL.md"
        dest.write_text(content, encoding="utf-8")
        installed[name] = str(dest)
    return installed


def uninstall_skill(target: str = "all") -> dict[str, bool]:
    removed: dict[str, bool] = {}
    for name, folder in _resolve_targets(target).items():
        if folder.exists():
            shutil.rmtree(folder)
            removed[name] = True
        else:
            removed[name] = False
    return removed


def skill_status() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, folder in SKILL_TARGETS.items():
        f = folder / "SKILL.md"
        out[name] = {
            "installed": f.exists(),
            "path": str(f),
        }
    return out


def install_mcp_config(
    target: str = "antigravity",
    *,
    executable: str | None = None,
    profile: str | None = None,
) -> Path:
    t = target.strip().lower()
    if t not in MCP_CONFIG_TARGETS:
        raise ValueError(
            f"Unknown MCP config target {target!r}. Choose from: {', '.join(MCP_CONFIG_TARGETS)}"
        )
    cfg_path, root_key = MCP_CONFIG_TARGETS[t]
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass

    cmd = executable or shutil.which("gflow-mcp") or "gflow-mcp"
    args = ["--transport", "stdio"]
    if profile:
        args.extend(["--profile", profile])

    servers = data.setdefault(root_key, {})
    servers["gflow"] = {
        "command": cmd,
        "args": args,
    }
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg_path
