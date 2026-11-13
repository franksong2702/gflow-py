"""Filesystem layout and profile resolution for gflow-py (`~/.gflow`)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .exceptions import ValidationError

HOME_ENV = "GFLOW_HOME"
PROFILE_ENV = "GFLOW_PROFILE"
STORAGE_ENV = "GFLOW_STORAGE_STATE"

_PROFILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def get_home_dir() -> Path:
    """Return the root data directory (`$GFLOW_HOME` or `~/.gflow`)."""
    override = os.environ.get(HOME_ENV, "").strip()
    base = Path(override).expanduser() if override else (Path.home() / ".gflow")
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def validate_profile_name(name: str) -> str:
    cleaned = name.strip()
    if not _PROFILE_RE.match(cleaned):
        raise ValidationError(
            f"Invalid profile name {name!r}: use 1..64 alphanumeric, '.', '_', or '-' characters."
        )
    return cleaned


def get_active_profile(explicit: str | None = None) -> str:
    """Resolve the active profile name from explicit arg, env, config, or 'default'."""
    if explicit:
        return validate_profile_name(explicit)
    env_val = os.environ.get(PROFILE_ENV, "").strip()
    if env_val:
        return validate_profile_name(env_val)
    cfg_file = get_home_dir() / "config.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text())
            if isinstance(data, dict) and isinstance(data.get("active_profile"), str):
                return validate_profile_name(data["active_profile"])
        except (OSError, ValueError):
            pass
    return "default"


def set_active_profile(profile: str) -> None:
    name = validate_profile_name(profile)
    cfg_file = get_home_dir() / "config.json"
    data: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass
    data["active_profile"] = name
    _atomic_write_json(cfg_file, data)


def get_profile_dir(profile: str | None = None, *, create: bool = True) -> Path:
    name = get_active_profile(profile)
    path = get_home_dir() / "profiles" / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def get_storage_state_path(profile: str | None = None, explicit_storage: str | Path | None = None) -> Path:
    if explicit_storage:
        return Path(explicit_storage).expanduser()
    env_storage = os.environ.get(STORAGE_ENV, "").strip()
    if env_storage:
        return Path(env_storage).expanduser()
    return get_profile_dir(profile) / "storage_state.json"


def get_browser_profile_dir(profile: str | None = None) -> Path:
    p = get_profile_dir(profile) / "browser_profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_context_path(profile: str | None = None) -> Path:
    return get_profile_dir(profile) / "context.json"


def load_context(profile: str | None = None) -> dict[str, Any]:
    path = get_context_path(profile)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_context(context: dict[str, Any], profile: str | None = None) -> None:
    _atomic_write_json(get_context_path(profile), context)


def get_default_output_dir() -> Path:
    override = os.environ.get("GFLOW_OUTPUT_DIR", "").strip()
    out = Path(override).expanduser() if override else (Path.home() / "Downloads" / "gflow")
    out.mkdir(parents=True, exist_ok=True)
    return out


def list_profiles() -> list[str]:
    profiles_root = get_home_dir() / "profiles"
    if not profiles_root.exists():
        return []
    return sorted(
        p.name for p in profiles_root.iterdir() if p.is_dir() and _PROFILE_RE.match(p.name)
    )


def _atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    os.replace(tmp, path)
