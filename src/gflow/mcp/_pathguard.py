"""Filesystem confinement helpers for the restricted (public HTTP) MCP transport."""

from __future__ import annotations

from pathlib import Path

from ..exceptions import ValidationError

__all__ = ["OUTSIDE_ROOT_MESSAGE", "SYMLINK_TARGET_MESSAGE", "resolve_confined_output"]

OUTSIDE_ROOT_MESSAGE = (
    "output_path must resolve to a file inside the server exports directory; "
    "pass a relative path such as clips/out.mp4."
)
SYMLINK_TARGET_MESSAGE = "output_path must not be a symbolic link."
_EMPTY_MESSAGE = "output_path must be a non-empty path inside the server exports directory."
_NUL_MESSAGE = "output_path must not contain NUL characters."


def resolve_confined_output(output_path: str, root: Path) -> Path:
    """Resolve ``output_path`` and guarantee it stays inside ``root``.

    - Empty / whitespace-only strings and strings containing NUL are rejected.
    - Only relative paths are accepted (interpreted relative to ``root``); absolute/anchored paths are rejected.
    - ``..`` components and symlinked directories are neutralised by resolving before the containment check.
    - The final path component must not itself be a symbolic link.

    Error messages never echo the real server-side path.
    """
    if not isinstance(output_path, str) or not output_path.strip():
        raise ValidationError(_EMPTY_MESSAGE)
    if "\x00" in output_path:
        raise ValidationError(_NUL_MESSAGE)

    try:
        root_resolved = Path(root).resolve(strict=False)
        raw = Path(output_path)
        if raw.is_absolute() or raw.anchor:
            # Never accept absolute paths (even inside root): they would let a caller probe the real layout.
            raise ValidationError(OUTSIDE_ROOT_MESSAGE)
        candidate = root_resolved / raw
        name = candidate.name
        if name in ("", ".", ".."):
            raise ValidationError(OUTSIDE_ROOT_MESSAGE)
        parent_resolved = candidate.parent.resolve(strict=False)
        # Containment first, symlink inspection second: nothing outside root is ever examined.
        if parent_resolved != root_resolved and not parent_resolved.is_relative_to(root_resolved):
            raise ValidationError(OUTSIDE_ROOT_MESSAGE)
        leaf = parent_resolved / name
        if leaf.is_symlink():
            raise ValidationError(SYMLINK_TARGET_MESSAGE)
        resolved = leaf.resolve(strict=False)
    except ValidationError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ValidationError(OUTSIDE_ROOT_MESSAGE) from None

    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise ValidationError(OUTSIDE_ROOT_MESSAGE)
    return resolved
