"""Allow `python -m gflow` execution."""

from __future__ import annotations

from .cli.main import cli

if __name__ == "__main__":
    cli()
