"""Tests for the small subprocess wrappers in frops.commands."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from frops.commands import capture_command


def test_capture_command_without_timeout_runs_normally() -> None:
    out, rc = capture_command("echo hello")
    assert rc == 0
    assert "hello" in out


def test_capture_command_with_satisfied_timeout_runs_normally() -> None:
    """A command that finishes well under the timeout returns its real output."""
    out, rc = capture_command("echo quick", timeout=5)
    assert rc == 0
    assert "quick" in out


def test_capture_command_returns_124_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock TimeoutExpired so the test doesn't actually wait for a real timeout."""

    def _fake_run(*_args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd="fake",
            timeout=float(kwargs.get("timeout") or 0.0),
            output="partial output before kill",
        )

    monkeypatch.setattr("frops.commands.subprocess.run", _fake_run)

    out, rc = capture_command("hang", timeout=20)
    assert rc == 124  # GNU `timeout` convention so callers can distinguish
    assert "partial output before kill" in out
    assert "(timed out after 20s)" in out


def test_capture_command_returns_124_on_timeout_with_bytes_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exc.output can be bytes when text=True isn't honored on early exit."""

    def _fake_run(*_args: Any, **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(
            cmd="fake",
            timeout=float(kwargs.get("timeout") or 0.0),
            output=b"binary partial \xff",
        )

    monkeypatch.setattr("frops.commands.subprocess.run", _fake_run)
    out, rc = capture_command("hang", timeout=20)
    assert rc == 124
    # Replacement char rendered for the invalid byte; the decoder must not crash.
    assert "binary partial" in out
    assert "(timed out after 20s)" in out
