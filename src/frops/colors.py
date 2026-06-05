"""Minimal ANSI color helpers used by the renderers.

Colors are disabled when:
  - `stdout` isn't a terminal (so `frops ... | less` / `| jq` stay clean), OR
  - the `NO_COLOR` env var is set (https://no-color.org/), OR
  - the `FROPS_NO_COLOR` env var is set (project-specific override that
    doesn't disable color for any other tools the user might have).

Set `FROPS_FORCE_COLOR=1` to force colors on (useful in scripts that
capture frops output to a file but still want ANSI codes preserved).
"""

from __future__ import annotations

import os
import sys

# DEC SGR escape sequences. Adding bold (`1;`) to make them readable on
# both light- and dark-background terminals without picking a specific
# 256-color or RGB shade.
_RESET = "\033[0m"
_PALETTE: dict[str, str] = {
    "yellow": "\033[1;33m",
    "cyan": "\033[1;36m",
    "magenta": "\033[1;35m",
    "dim": "\033[2m",
}


def supports_color() -> bool:
    """Whether the current run should emit ANSI escapes."""
    if os.environ.get("FROPS_FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FROPS_NO_COLOR"):
        return False
    return sys.stdout.isatty()


def color(text: str, name: str) -> str:
    """Wrap `text` in the named color's escape sequence; passthrough when off."""
    if not text or not supports_color():
        return text
    code = _PALETTE.get(name)
    if code is None:
        return text
    return f"{code}{text}{_RESET}"


def yellow(text: str) -> str:
    """BMN names — match the operator's mental model of \"the asset I'm looking at\"."""
    return color(text, "yellow")


def cyan(text: str) -> str:
    """HO ticket keys / JIRA links — visually distinct from BMN identifiers."""
    return color(text, "cyan")


def magenta(text: str) -> str:
    """DEVICESLOT — physical location, picked to stand apart from logical asset color."""
    return color(text, "magenta")


def dim(text: str) -> str:
    """Lower-attention values (e.g. placeholders like `(none)`)."""
    return color(text, "dim")
