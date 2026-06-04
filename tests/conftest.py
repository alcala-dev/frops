"""Shared pytest fixtures and defaults.

Notably: `frops.cli.run_command_filter_columns` and `frops.cli.run_command`
are stubbed by default so tests don't accidentally shell out to kubectl
when a `main(["view", ...])` call runs. Individual tests that *want* to
verify the runner is invoked re-bind these via `monkeypatch.setattr`,
which takes precedence over the autouse fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_shell_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `frops.cli.run_command*` to no-op rc=0.

    Without this, the SKU-view path in `handle_view` would attempt a real
    `kubectl get bmns -o wide ...` subprocess. In CI (no kubectl on PATH)
    that returns rc=1, breaking every `main(["view", "sku", ...])` test.
    Tests asserting on call count / args re-bind these to their own
    fakes, which silently overrides this default.
    """
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("frops.cli.run_command_filter_columns", lambda _cmd, _keep: 0)
