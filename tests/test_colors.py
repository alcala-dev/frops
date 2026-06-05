"""Tests for the small ANSI color helpers in frops.colors."""

from __future__ import annotations

import pytest

from frops import colors


def test_supports_color_off_when_stdout_is_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    # capsys / capfd already redirect stdout to a non-TTY in tests, but be
    # explicit so the assertion is meaningful when run interactively too.
    monkeypatch.setattr("frops.colors.sys.stdout.isatty", lambda: False)
    monkeypatch.delenv("FROPS_FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FROPS_NO_COLOR", raising=False)
    assert colors.supports_color() is False


def test_supports_color_off_when_no_color_env_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.colors.sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FROPS_FORCE_COLOR", raising=False)
    assert colors.supports_color() is False


def test_supports_color_off_when_frops_no_color_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frops.colors.sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FROPS_FORCE_COLOR", raising=False)
    monkeypatch.setenv("FROPS_NO_COLOR", "1")
    assert colors.supports_color() is False


def test_supports_color_on_when_force_color_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # FROPS_FORCE_COLOR wins over both isatty=False and NO_COLOR being set.
    monkeypatch.setattr("frops.colors.sys.stdout.isatty", lambda: False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FROPS_FORCE_COLOR", "1")
    assert colors.supports_color() is True


def test_color_passes_text_through_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.colors.supports_color", lambda: False)
    assert colors.yellow("ss-bmn") == "ss-bmn"
    assert colors.cyan("HO-1") == "HO-1"
    assert colors.magenta("slot-1") == "slot-1"
    assert colors.dim("(none)") == "(none)"


def test_color_wraps_with_escape_sequences_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frops.colors.supports_color", lambda: True)
    out = colors.yellow("ss-bmn")
    assert "\033[1;33m" in out  # yellow open
    assert "ss-bmn" in out
    assert out.endswith("\033[0m")  # reset


def test_color_passes_empty_text_through_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wrapping an empty string in escapes would emit two zero-width sequences
    # back-to-back; just pass through to keep stdout clean.
    monkeypatch.setattr("frops.colors.supports_color", lambda: True)
    assert colors.yellow("") == ""


def test_color_unknown_name_falls_back_to_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frops.colors.supports_color", lambda: True)
    assert colors.color("text", "no-such-color") == "text"
