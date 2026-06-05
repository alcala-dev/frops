"""Shared pytest fixtures and defaults.

Notably: `frops.cli.run_command` is stubbed by default so tests don't
accidentally shell out to kubectl when a `main(["view", ...])` call
runs. Individual tests that want to verify the runner is invoked
re-bind via `monkeypatch.setattr`, which takes precedence over the
autouse fixture.
"""

from __future__ import annotations

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
