"""Tests for the at(1) wrapper."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from frops.schedule import at_available, schedule_at


def _fake_completed(
    *, returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["at"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_at_available_reflects_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "frops.schedule.shutil.which",
        lambda name: "/usr/bin/at" if name == "at" else None,
    )
    assert at_available() is True

    monkeypatch.setattr("frops.schedule.shutil.which", lambda _name: None)
    assert at_available() is False


def test_schedule_at_returns_at_confirmation_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.schedule.at_available", lambda: True)

    captured: dict[str, Any] = {}

    def _fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        captured["timeout"] = kwargs.get("timeout")
        # at(1) writes "warning: …" + "job N at …" to stderr on success.
        return _fake_completed(
            returncode=0,
            stderr="warning: commands will be executed using /bin/sh\njob 42 at Wed Jun 10 21:30:00 2026",
        )

    monkeypatch.setattr("frops.schedule.subprocess.run", _fake_run)

    ok, detail = schedule_at("echo hi", "now + 15 minutes")
    assert ok is True
    assert "job 42" in detail
    assert captured["args"] == ["at", "now + 15 minutes"]
    assert captured["input"] == "echo hi"


def test_schedule_at_returns_failure_when_at_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.schedule.at_available", lambda: False)

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("subprocess.run should not be called when at is missing")

    monkeypatch.setattr("frops.schedule.subprocess.run", _explode)

    ok, detail = schedule_at("echo hi", "now + 15 minutes")
    assert ok is False
    assert "not found" in detail


def test_schedule_at_returns_failure_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.schedule.at_available", lambda: True)
    monkeypatch.setattr(
        "frops.schedule.subprocess.run",
        lambda *_a, **_k: _fake_completed(returncode=1, stderr="atd is not running"),
    )

    ok, detail = schedule_at("echo hi", "now + 15 minutes")
    assert ok is False
    assert "atd is not running" in detail
    assert "returned 1" in detail


def test_schedule_at_falls_back_to_stdout_when_stderr_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frops.schedule.at_available", lambda: True)
    monkeypatch.setattr(
        "frops.schedule.subprocess.run",
        lambda *_a, **_k: _fake_completed(returncode=2, stdout="something went wrong", stderr=""),
    )

    ok, detail = schedule_at("echo hi", "now + 15 minutes")
    assert ok is False
    assert "something went wrong" in detail


def test_schedule_at_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.schedule.at_available", lambda: True)

    def _raise_timeout(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="at", timeout=10.0)

    monkeypatch.setattr("frops.schedule.subprocess.run", _raise_timeout)
    ok, detail = schedule_at("echo hi", "now + 15 minutes")
    assert ok is False
    assert "timed out" in detail


def test_schedule_at_reports_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.schedule.at_available", lambda: True)

    def _raise_os(*_a: Any, **_k: Any) -> Any:
        raise OSError("permission denied")

    monkeypatch.setattr("frops.schedule.subprocess.run", _raise_os)
    ok, detail = schedule_at("echo hi", "now + 15 minutes")
    assert ok is False
    assert "permission denied" in detail
