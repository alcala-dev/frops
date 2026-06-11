"""Thin wrapper around `at(1)` for scheduling delayed shell commands.

frops is a one-shot CLI, so when a workflow needs a delayed follow-up
(today: the node-zap rerun 15 minutes after a CW0912 power-drain) we
hand the work to the host's own scheduler and exit. No internal daemon,
no persistent state.

Caveats the caller is responsible for surfacing to the operator:
  - The at job runs as the scheduling user. If the user's tsh / Doppler
    session has expired by the time the job fires, the scheduled cwctl
    command will fail with an auth error. There's nothing this module
    can do about that — credentials have their own lifetimes.
  - On macOS, `atd` is disabled by default. The schedule call returns a
    descriptive failure in that case so the caller can fall back to
    printing the command for manual execution.
"""

from __future__ import annotations

import shutil
import subprocess

# `at` exits fast in the happy path (writes "job <N> at <date>" to
# stderr). 10s is generous; anything longer suggests the daemon is
# stuck and we should bail rather than block the operator's terminal.
DEFAULT_TIMEOUT_S: float = 10.0


def at_available() -> bool:
    """True when the `at` binary is reachable on PATH.

    Separate from the schedule call so callers can short-circuit and
    print a manual-run reminder without a noisy subprocess invocation.
    """
    return shutil.which("at") is not None


def schedule_at(
    command: str,
    when: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[bool, str]:
    """Pipe `command` into `at <when>`. Returns (success, detail).

    `detail` carries the at(1) confirmation line on success (typically
    "job 42 at Wed Jun 10 21:30:00 2026") so the operator can later
    `atq` / `at -c <id>` to verify or cancel. On failure `detail` is
    the captured error message — at not installed, atd not running,
    subprocess timeout, or non-zero exit.
    """
    if not at_available():
        return False, "`at` command not found on this host"
    try:
        proc = subprocess.run(
            ["at", when],
            input=command,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"`at {when}` timed out after {timeout}s"
    except OSError as exc:
        return False, f"`at` invocation failed: {exc}"
    if proc.returncode != 0:
        # at(1) writes errors to stderr; fall back to stdout when stderr is empty.
        err = (proc.stderr or proc.stdout).strip() or "(no output)"
        return False, f"at(1) returned {proc.returncode}: {err}"
    # at(1) writes the "job <N> at <date>" confirmation to stderr by design.
    detail = (proc.stderr or proc.stdout).strip()
    return True, detail
