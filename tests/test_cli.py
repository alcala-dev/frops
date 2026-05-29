"""Tests for argparse wiring and pure helpers in frops.cli."""

from __future__ import annotations

import pytest

from frops.cli import build_command, build_parser, format_section, main

# ----------------------------- build_command --------------------------------


def test_build_command_without_user_filter_is_passthrough() -> None:
    cmd = build_command("fails", None)
    assert cmd == "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail'"


def test_build_command_splices_user_filter_inside_quotes() -> None:
    cmd = build_command("fails", "jdoe")
    assert cmd.endswith("ownership.coreweave.com/owner=jdoe'")
    # The whole selector must remain a single quoted kubectl argument.
    assert cmd.count("'") == 2


def test_build_command_unknown_type_raises() -> None:
    with pytest.raises(KeyError):
        build_command("not-a-real-type", None)


# ----------------------------- format_section -------------------------------


def test_format_section_zero_exit_has_no_status() -> None:
    rendered = format_section("Overview", "kubectl get bmns x", "row1\nrow2", 0)
    assert "### Overview ###" in rendered
    assert "[exit" not in rendered
    assert "row1" in rendered and "row2" in rendered


def test_format_section_nonzero_exit_shows_status() -> None:
    rendered = format_section("Overview", "kubectl get bmns x", "boom", 1)
    assert "[exit 1]" in rendered


def test_format_section_empty_output_is_placeholder() -> None:
    rendered = format_section("Overview", "kubectl get bmns x", "", 0)
    assert "(no output)" in rendered


# ----------------------------- argparse -------------------------------------


def test_parser_rejects_unknown_fail_type() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["view", "definitely-not-a-real-type"])


def test_parser_view_accepts_user_filter() -> None:
    parser = build_parser()
    args = parser.parse_args(["view", "fails", "-u", "jdoe"])
    assert args.fail_type == "fails"
    assert args.user_filter == "jdoe"


def test_parser_analyze_requires_name() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["analyze", "bmn"])


# ----------------------------- main -----------------------------------------


def test_main_with_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "usage:" in captured.out.lower()


def test_main_view_dry_run_does_not_invoke_subprocess(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    rc = main(["--dry-run", "view", "fails"])
    assert rc == 0
    assert calls == [], "dry-run must not execute the command"
    out = capsys.readouterr().out
    assert "Viewing: fails" in out


def test_main_analyze_dry_run_skips_capture(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_capture(cmd: str) -> tuple[str, int]:
        calls.append(cmd)
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", fake_capture)
    rc = main(["--dry-run", "analyze", "bmn", "ss929610x4724071"])
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "Analyzing bmn: ss929610x4724071" in out
    assert "(dry-run, not executed)" in out
