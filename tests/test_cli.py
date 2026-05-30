"""Tests for argparse wiring and pure helpers in frops.cli."""

from __future__ import annotations

import pytest

from frops.cli import (
    build_command,
    build_parser,
    build_sku_command,
    format_section,
    main,
)

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


# ----------------------------- build_sku_command ----------------------------


def test_build_sku_command_renders_sku_and_excluded_states() -> None:
    cmd = build_sku_command("GPU-GH200-01", None)
    assert "ds.coreweave.com/sku.cw-sku=GPU-GH200-01" in cmd
    assert "flcc.coreweave.com/state notin (production,ready,rma,broken,dev,debug)" in cmd
    # Selector remains a single quoted argument.
    assert cmd.count("'") == 2


def test_build_sku_command_splices_user_filter() -> None:
    cmd = build_sku_command("GPU-GH200-01", "jdoe")
    assert cmd.endswith("ownership.coreweave.com/owner=jdoe'")
    assert "GPU-GH200-01" in cmd
    assert cmd.count("'") == 2


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
    assert args.sku_value is None


def test_parser_view_sku_captures_value() -> None:
    parser = build_parser()
    args = parser.parse_args(["view", "sku", "GPU-GH200-01"])
    assert args.fail_type == "sku"
    assert args.sku_value == "GPU-GH200-01"


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


def test_main_view_sku_dry_run_renders_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    rc = main(["--dry-run", "view", "sku", "GPU-GH200-01"])
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "Viewing: sku=GPU-GH200-01" in out
    assert "ds.coreweave.com/sku.cw-sku=GPU-GH200-01" in out


def test_main_view_sku_missing_value_errors(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    rc = main(["view", "sku"])
    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "requires a SKU argument" in err


def test_main_view_fail_type_with_extra_positional_errors(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    rc = main(["view", "fails", "GPU-GH200-01"])
    assert rc == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "takes no extra positional argument" in err


def test_main_analyze_dry_run_skips_subprocess(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    rc = main(["--dry-run", "analyze", "bmn", "ss929610x4724071"])
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "Analyzing bmn: ss929610x4724071" in out
    assert "(dry-run, not executed)" in out


def test_main_analyze_streams_each_step(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Analyze must invoke run_command (not capture_command) so colors flow."""
    calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    rc = main(["analyze", "bmn", "ss929610x4724071"])
    assert rc == 0
    # One run_command per ANALYZE_COMMANDS step for the 'bmn' target.
    assert len(calls) == 4
    assert all("ss929610x4724071" in c for c in calls)
    out = capsys.readouterr().out
    assert "### Overview ###" in out


def test_main_analyze_propagates_worst_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rcs = iter([0, 2, 0, 1])
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: next(rcs))
    rc = main(["analyze", "bmn", "ss929610x4724071"])
    assert rc == 2
