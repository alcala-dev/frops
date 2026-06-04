"""Thin wrappers around subprocess for streaming and capturing shell commands."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable


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


def run_command_filter_columns(
    command: str,
    keep_columns: Iterable[str],
) -> int:
    """
    Run a kubectl-style command, filter its wide-format columns down to
    `keep_columns` (matched by header name), and print the trimmed table.

    Trade-off vs. plain `run_command`: capturing the output disables the
    child's TTY detection, so colorizers like `kubecolor` won't apply.
    Returns the underlying subprocess exit code; on non-zero the raw
    output is forwarded as-is so error messages aren't lost.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error running command: {exc}", file=sys.stderr)
        return 1

    raw = result.stdout
    if result.returncode != 0 or not raw:
        # Pass through error messages / empty output verbatim — column
        # filtering only makes sense for a well-formed kubectl table.
        if raw:
            print(raw, end="" if raw.endswith("\n") else "\n")
        return result.returncode

    filtered = _filter_table_columns(raw, tuple(keep_columns))
    print(filtered, end="" if filtered.endswith("\n") else "\n")
    return result.returncode


def _filter_table_columns(table: str, keep: tuple[str, ...]) -> str:
    """Subset a kubectl-style wide-format table by header name.

    Rows split on 2+ whitespace (kubectl's column separator) — robust to
    values that overflow their header's display width. Column widths are
    recomputed from the surviving cells so the trimmed table stays
    aligned. Unknown header names in `keep` are silently dropped so
    catalog changes don't blow up against older CRD outputs.
    """
    lines = table.splitlines()
    if not lines:
        return table

    header_line = next((line for line in lines if line.strip()), "")
    if not header_line:
        return table
    header_idx = lines.index(header_line)

    headers = re.findall(r"\S+", header_line)
    keep_indices = [headers.index(name) for name in keep if name in headers]
    if not keep_indices:
        # Asked-for columns don't appear in this table — pass through.
        return table

    rows: list[list[str] | None] = []
    for line in lines[header_idx + 1 :]:
        if not line.strip():
            rows.append(None)
            continue
        values = re.split(r"\s{2,}", line.rstrip())
        if max(keep_indices) >= len(values):
            rows.append(None)
            continue
        rows.append([values[i] for i in keep_indices])

    kept_headers = [headers[i] for i in keep_indices]
    widths = [len(h) for h in kept_headers]
    for row in rows:
        if row is None:
            continue
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))

    out: list[str] = []
    out.append("  ".join(h.ljust(w) for h, w in zip(kept_headers, widths, strict=True)))
    for row in rows:
        if row is None:
            out.append("")
        else:
            out.append("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    return "\n".join(out)


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
