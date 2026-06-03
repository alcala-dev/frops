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


def capture_command(command: str, timeout: float | None = None) -> tuple[str, int]:
    """
    Execute a shell command, capturing and returning its combined stdout+stderr output.

    Args:
        command: Shell command to run.
        timeout: Optional wall-clock timeout in seconds. When the timeout
            fires, the child process is killed and the call returns
            `(partial_output_with_marker, 124)` — exit code 124 follows
            GNU `timeout`'s convention for "command timed out" so callers
            can distinguish a timeout from a regular non-zero exit.

    Returns a (output, exit_code) tuple so callers can act on failures if needed.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills the child on timeout. exc.output may contain
        # partial bytes/text captured before the deadline — surface it so
        # downstream parsers can still see what little they got.
        raw = exc.output
        partial = raw.decode(errors="replace") if isinstance(raw, bytes) else raw or ""
        marker = f"(timed out after {timeout}s)"
        return (f"{partial}\n{marker}".strip(), 124)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return "", 130
    except Exception as exc:
        return f"Error running command: {exc}", 1
