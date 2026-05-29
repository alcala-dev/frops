"""Thin wrappers around subprocess for streaming and capturing shell commands."""

from __future__ import annotations

import subprocess
import sys


def run_command(command: str) -> int:
    """
    Execute a shell command, streaming output directly to the terminal.

    Returns the exit code so callers can act on failures if needed.
    """
    try:
        result = subprocess.run(command, shell=True)
        return result.returncode
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error running command: {exc}", file=sys.stderr)
        return 1


def capture_command(command: str) -> tuple[str, int]:
    """
    Execute a shell command, capturing and returning its combined stdout+stderr output.
    Returns a (output, exit_code) tuple so callers can act on failures if needed.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return result.stdout, result.returncode
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return "", 130
    except Exception as exc:
        return f"Error running command: {exc}", 1
