"""Tests for the small subprocess wrappers in frops.commands."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from frops.commands import _filter_table_columns, capture_command, run_command_filter_columns


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


# --------------------------- _filter_table_columns --------------------------


_WIDE_OUTPUT = (
    "NAME       DEVICESLOT        CW-NODE   ONLINE   STATE   TS    ORG-ID\n"
    "bmn-a      slot-1            g111aaa   true     fail    1d    org-x\n"
    "bmn-b      slot-2            g222bbb   false    triage  5h    org-y\n"
)


def test_filter_table_columns_keeps_only_requested_and_in_order() -> None:
    out = _filter_table_columns(_WIDE_OUTPUT, ("NAME", "STATE", "TS"))
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "STATE", "TS"]
    assert lines[1].split() == ["bmn-a", "fail", "1d"]
    assert lines[2].split() == ["bmn-b", "triage", "5h"]


def test_filter_table_columns_drops_unknown_headers_silently() -> None:
    # "NOPE" isn't in the input; should be skipped, ignoring the rest is fine.
    out = _filter_table_columns(_WIDE_OUTPUT, ("NAME", "NOPE", "STATE"))
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "STATE"]
    assert lines[1].split() == ["bmn-a", "fail"]


def test_filter_table_columns_passes_through_when_no_columns_match() -> None:
    out = _filter_table_columns(_WIDE_OUTPUT, ("DOES-NOT-EXIST",))
    # Nothing in `keep` matched; preserve the raw table verbatim.
    assert out == _WIDE_OUTPUT


def test_filter_table_columns_handles_value_overflow() -> None:
    # STATE value is wider than the header; the multi-space split must
    # still attribute it correctly.
    overflow = "NAME   STATE                   TS\nbmn-1  production-reboot-fail  2h\n"
    out = _filter_table_columns(overflow, ("NAME", "TS"))
    lines = out.splitlines()
    assert lines[1].split() == ["bmn-1", "2h"]


def test_filter_table_columns_empty_input_round_trips() -> None:
    assert _filter_table_columns("", ("NAME",)) == ""


# --------------------------- run_command_filter_columns ---------------------


def test_run_command_filter_columns_prints_filtered_table(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: subprocess succeeds, output is filtered down to `keep`."""

    class _Result:
        returncode = 0
        stdout = _WIDE_OUTPUT

    monkeypatch.setattr("frops.commands.subprocess.run", lambda *_a, **_k: _Result())
    rc = run_command_filter_columns("fake cmd", ("NAME", "STATE"))
    captured = capsys.readouterr().out
    assert rc == 0
    assert "NAME" in captured
    assert "STATE" in captured
    assert "DEVICESLOT" not in captured  # filtered out


def test_run_command_filter_columns_passes_through_error_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On non-zero rc, the raw stderr-equivalent text is forwarded so the
    operator sees the actual error message instead of an empty screen."""

    class _Result:
        returncode = 7
        stdout = "error: forbidden\n"

    monkeypatch.setattr("frops.commands.subprocess.run", lambda *_a, **_k: _Result())
    rc = run_command_filter_columns("fake cmd", ("NAME",))
    captured = capsys.readouterr().out
    assert rc == 7
    assert "error: forbidden" in captured


def test_run_command_filter_columns_handles_subprocess_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError (e.g. command not found) is caught and surfaces as rc=1."""

    def _raise(*_a: Any, **_k: Any) -> None:
        raise OSError("command not found")

    monkeypatch.setattr("frops.commands.subprocess.run", _raise)
    rc = run_command_filter_columns("definitely-not-a-real-cmd", ("NAME",))
    assert rc == 1
