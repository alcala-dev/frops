"""Shared pytest fixtures and defaults.

Notably: `frops.cli.run_command` is stubbed by default so tests don't
accidentally shell out to kubectl when a `main(["view", ...])` call
runs. Individual tests that want to verify the runner is invoked
re-bind via `monkeypatch.setattr`, which takes precedence over the
autouse fixture.

`FROPS_STATE_DIR` is also redirected to a per-test tmp dir so CW0912
state file reads don't leak between tests (or pick up stale state
from a developer's `~/.cache/frops/cw0912/` directory).

`AWX_USERNAME` / `AWX_PASSWORD` are also cleared by default so the
awxstat invocation builder consistently picks the `awxstat …` literal
form across local + CI runs. Tests that need to exercise the
env-bypass shim path re-set the vars locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stub_shell_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `frops.cli.run_command` to no-op rc=0.

    Without this, the view path in `handle_view` would attempt a real
    `kubectl get bmns ...` subprocess. In CI (no kubectl on PATH) that
    returns non-zero, breaking every `main(["view", ...])` test that
    asserts `rc == 0`. Tests asserting on call count / args re-bind
    this to their own fake, which silently overrides this default.
    """
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)


@pytest.fixture(autouse=True)
def _isolate_cw0912_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect CW0912 state file IO at a per-test temp directory.

    The CW0912 state machine reads `~/.cache/frops/cw0912/<bmn>.json`
    by default; a developer running tests locally could otherwise see
    state from their last fleet run leak into the test fixtures.
    Tests that want to set up specific state files re-bind via
    `monkeypatch.setenv("FROPS_STATE_DIR", str(some_path))`, which
    takes precedence over this default.
    """
    monkeypatch.setenv("FROPS_STATE_DIR", str(tmp_path / "frops-cw0912"))


@pytest.fixture(autouse=True)
def _isolate_awx_env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear AWX_USERNAME / AWX_PASSWORD by default.

    `frops.awx_invoker.awxstat_command` switches to the envbypass shim
    when both vars are present in env. A developer running tests with
    those vars exported (the common case once frops is set up) would
    otherwise see the integration tests' `cmd.startswith("awxstat")`
    fixtures miss the awxstat dispatch and report rc=1 for unrelated
    reasons. Tests that exercise the shim path re-set the vars locally.
    """
    monkeypatch.delenv("AWX_USERNAME", raising=False)
    monkeypatch.delenv("AWX_PASSWORD", raising=False)
