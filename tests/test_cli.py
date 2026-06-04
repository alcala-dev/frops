"""Tests for argparse wiring and pure helpers in frops.cli."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from frops.cli import (
    build_command,
    build_parser,
    build_sku_command,
    build_sku_command_json,
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


# ----------------------------- build_sku_command_json -----------------------


def test_build_sku_command_json_uses_json_output_format() -> None:
    cmd = build_sku_command_json("GPU-GH200-01", None)
    assert "-o json" in cmd
    assert "-o wide" not in cmd
    assert "ds.coreweave.com/sku.cw-sku=GPU-GH200-01" in cmd
    assert cmd.count("'") == 2


# ----------------------------- view sku --action ----------------------------


_BMN_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "ss900770x4200980",
                    "labels": {"ds.coreweave.com/sku.cw-sku": "GPU-GH200-01"},
                },
                "status": {"reportedNodeInfo": {"nodeName": "g81b512"}},
            },
            {
                "metadata": {
                    "name": "ss111111x1111111",
                    "labels": {"ds.coreweave.com/sku.cw-sku": "GPU-GH200-01"},
                },
                # No nodeName — should be skipped.
                "status": {"reportedNodeInfo": {}},
            },
        ]
    }
)


def _fake_capture_for_action(
    awx_outputs: dict[tuple[str, str], tuple[str, int]],
    json_output: tuple[str, int] = (_BMN_JSON, 0),
) -> Callable[[str], tuple[str, int]]:
    """Build a capture_command stand-in keyed by (limit_type, bmn) or 'json'."""

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        if "-o json" in cmd:
            return json_output
        # awxstat -l <type> <BMN>
        parts = cmd.split()
        if parts[:2] == ["awxstat", "-l"]:
            limit_type = parts[2]
            bmn = parts[3]
            return awx_outputs.get((limit_type, bmn), ("", 0))
        return ("", 0)

    return _capture


def _awx_with_code(code: str = "CW0211") -> str:
    return (
        "Node:       S900770X4200980\n"
        "Job Status: failed\n\n"
        "cw_error_codes={\n"
        f"  {code}: a description\n"
        "}\n"
    )


def test_main_view_sku_action_without_yes_aborts_on_empty_input(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty answer at the group-selection prompt aborts cleanly."""
    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): (_awx_with_code(), 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        }
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== Planned actions ===" in out
    assert "[power-drain] 1 node(s)" in out
    assert "Aborted by user" in out
    assert "=== Execution summary ===" not in out


def test_main_view_sku_action_without_yes_executes_on_all_selection(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`a` at the prompt picks every available group (power-drain here)."""
    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): (_awx_with_code(), 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        }
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "a")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any(
        "cwctl flcc node --one-off" in c and "power-drain ss900770x4200980" in c for c in run_calls
    ), run_calls
    assert "=== Execution summary ===" in out
    assert "1 succeeded, 0 failed" in out


def test_main_view_sku_action_dry_run_skips_action_pipeline(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_calls: list[str] = []

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        capture_calls.append(cmd)
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", _capture)
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)

    rc = main(["--dry-run", "view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    assert capture_calls == [], "dry-run must not run kubectl JSON or awxstat"
    assert "would also fetch JSON BMN data" in out


def test_main_action_rejected_on_non_sku_view(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr(
        "frops.cli.capture_command",
        lambda _cmd: ("should not be called", 0),
    )

    rc = main(["view", "fails", "--action"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--action is only supported with 'view sku'" in err


def test_main_action_handles_kubectl_json_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "frops.cli.capture_command",
        lambda _cmd: ("error: forbidden\n", 7),
    )
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    err = capsys.readouterr().err
    assert rc == 7
    assert "failed to fetch BMN JSON" in err
    assert "forbidden" in err


# ----------------------------- view sku --action --yes (Phase B) ------------


def _input_should_not_be_called(_prompt: str) -> str:
    raise AssertionError("--yes must skip the confirmation prompt")


def test_main_view_sku_action_yes_executes_without_prompt(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): (_awx_with_code(), 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        }
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("builtins.input", _input_should_not_be_called)

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any("power-drain ss900770x4200980" in c for c in run_calls)
    assert "=== Execution summary ===" in out


def test_main_view_sku_action_yes_reports_worst_rc_on_partial_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two BMNs, both eligible for power-drain. Make one cwctl call fail.
    two_bmn_json = json.dumps(
        {
            "items": [
                {
                    "metadata": {
                        "name": "bmn-a",
                        "labels": {"ds.coreweave.com/sku.cw-sku": "GPU-GH200-01"},
                    },
                    "status": {"reportedNodeInfo": {"nodeName": "g-a"}},
                },
                {
                    "metadata": {
                        "name": "bmn-b",
                        "labels": {"ds.coreweave.com/sku.cw-sku": "GPU-GH200-01"},
                    },
                    "status": {"reportedNodeInfo": {"nodeName": "g-b"}},
                },
            ]
        }
    )
    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "bmn-a"): (_awx_with_code(), 0),
            ("bmc", "bmn-a"): ("cw_error_codes={}\n", 0),
            ("mgmt", "bmn-b"): (_awx_with_code(), 0),
            ("bmc", "bmn-b"): ("cw_error_codes={}\n", 0),
        },
        json_output=(two_bmn_json, 0),
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)

    # First run_command call is the wide kubectl display (rc=0). The next two
    # are the cwctl actions — make the second fail with rc=5.
    rcs = iter([0, 0, 5])
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: next(rcs))

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 5, "worst rc across executed actions should propagate"
    assert "1 succeeded, 1 failed" in out
    assert "exit 5" in out


def test_main_view_sku_action_yes_with_only_noops_runs_access_check(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --yes and NOOP-clean BMNs only, access check fires (it's part
    of 'all'); no cwctl actions run because nothing is actionable."""
    # No CW codes in either awxstat output → classifier returns NOOP-clean.
    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        }
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("builtins.input", _input_should_not_be_called)

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    # Only the wide kubectl view streams; no cwctl follow-ups.
    assert len(run_calls) == 1
    # Access check IS run under --yes when NOOP-clean targets exist.
    assert "=== Access check (NOOP + missing CW-NODE) ===" in out
    # No execution summary because no actionable cwctl commands ran.
    assert "=== Execution summary ===" not in out


def test_main_yes_without_action_is_rejected(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("frops.cli.capture_command", lambda _cmd: ("should-not-run", 0))
    rc = main(["view", "sku", "GPU-GH200-01", "--yes"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--yes requires --action" in err


# ----------------------------- view sku --action JIRA HO-ticket -------------


_HO_BMN_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "ss900770x4200980",
                    "labels": {
                        "ds.coreweave.com/sku.cw-sku": "GPU-GH200-01",
                        "ds.coreweave.com/status.asset.serial": "S900770X4200980",
                    },
                },
                "status": {"reportedNodeInfo": {"nodeName": "g81b512"}},
            }
        ]
    }
)


def _awx_with_ho_code() -> str:
    return (
        "Node:       S900770X4200980\n"
        "Job Status: failed\n\n"
        "cw_error_codes={\n"
        "  CW0201: hardware fault\n"
        "}\n"
    )


def test_main_view_sku_action_resolves_ho_ticket_when_jira_creds_present(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "me@x.com")
    monkeypatch.setenv("JIRA_TOKEN", "tok")

    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): (_awx_with_ho_code(), 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        },
        json_output=(_HO_BMN_JSON, 0),
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )

    # Stub the JIRA client: search returns one issue; append succeeds.
    search_args: list[str] = []
    update_calls: list[tuple[str, str]] = []

    class _FakeJIRA:
        def __init__(self) -> None:
            pass

        def search(self, jql: str) -> list[object]:
            search_args.append(jql)
            from frops.jira import JIRAIssue

            return [JIRAIssue(key="HO-12345", summary="...", status="Awaiting Support")]

        def append_to_description(self, key: str, block: str) -> None:
            update_calls.append((key, block))

    monkeypatch.setattr("frops.cli.JIRAClient", _FakeJIRA)

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0

    # JQL contains every identifier we can match HO summaries with.
    assert len(search_args) == 1
    for ident in ("ss900770x4200980", "g81b512", "S900770X4200980"):
        assert ident in search_args[0]

    # The plan should mention the resolved ticket, not the cwctl fallback.
    assert "append to HO-12345 description" in out
    assert "return-to-triage ss900770x4200980" not in out

    # Execution went through append_to_description, NOT run_command.
    assert update_calls and update_calls[0][0] == "HO-12345"
    assert "ss900770x4200980" in update_calls[0][1]
    assert "CW0201" in update_calls[0][1]
    # The only run_command call should be the initial kubectl wide view.
    assert len(run_calls) == 1
    assert "kubectl" in run_calls[0]


def test_main_view_sku_action_falls_back_to_cwctl_when_no_jira_match(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "me@x.com")
    monkeypatch.setenv("JIRA_TOKEN", "tok")

    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): (_awx_with_ho_code(), 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        },
        json_output=(_HO_BMN_JSON, 0),
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)
    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )

    class _FakeJIRA:
        def __init__(self) -> None:
            pass

        def search(self, _jql: str) -> list[object]:
            return []  # no matches

        def append_to_description(self, _key: str, _block: str) -> None:
            raise AssertionError("should not be called when no match")

    monkeypatch.setattr("frops.cli.JIRAClient", _FakeJIRA)

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    # Plan and execution both use the cwctl return-to-triage fallback.
    assert "return-to-triage" in out
    assert any("return-to-triage ss900770x4200980" in c for c in run_calls)


def test_main_view_sku_action_continues_without_jira_when_creds_missing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    captures = _fake_capture_for_action(
        awx_outputs={
            ("mgmt", "ss900770x4200980"): (_awx_with_ho_code(), 0),
            ("bmc", "ss900770x4200980"): ("cw_error_codes={}\n", 0),
        },
        json_output=(_HO_BMN_JSON, 0),
    )
    monkeypatch.setattr("frops.cli.capture_command", captures)
    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    captured = capsys.readouterr()
    assert rc == 0
    # User sees a one-line note explaining JIRA was skipped.
    assert "JIRA lookup disabled" in captured.err
    # Falls back to cwctl execution path.
    assert "return-to-triage" in captured.out
    assert any("return-to-triage ss900770x4200980" in c for c in run_calls)


# ----------------------------- view sku --action access check ---------------


_NOOP_BMN_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "ss900770x4200980",
                    "labels": {
                        "ds.coreweave.com/sku.cw-sku": "GPU-GH200-01",
                        "flcc.coreweave.com/workflow": "provision-v2",
                        "flcc.coreweave.com/workflow-step": "dpu-vaultify",
                        "flcc.coreweave.com/state": "fail",
                    },
                },
                "status": {"reportedNodeInfo": {"nodeName": "g826cb0"}},
            }
        ]
    }
)


def _fake_bmns_wide_for(bmn: str, ts: str = "10m") -> str:
    return f"NAME    STATE  TS\n{bmn}  fail   {ts}\n"


_MIXED_BMN_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "bmn-with-node",
                    "labels": {
                        "ds.coreweave.com/sku.cw-sku": "GPU-GH200-01",
                        "flcc.coreweave.com/workflow": "provision-v2",
                        "flcc.coreweave.com/workflow-step": "dpu-vaultify",
                        "flcc.coreweave.com/state": "fail",
                    },
                },
                "status": {"reportedNodeInfo": {"nodeName": "g123abc"}},
            },
            {
                "metadata": {
                    "name": "bmn-no-node",
                    "labels": {
                        "ds.coreweave.com/sku.cw-sku": "GPU-GH200-01",
                        "flcc.coreweave.com/workflow": "provision-v2",
                        "flcc.coreweave.com/workflow-step": "fielddiag",
                        "flcc.coreweave.com/state": "fail",
                    },
                },
                # No nodeName → ends up in the missing-CW-NODE bucket.
                "status": {"reportedNodeInfo": {}},
            },
        ]
    }
)


def test_main_view_sku_action_renders_missing_cwnode_section_and_includes_in_access_pool(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CW-NODE-less BMNs get their own table above the plan AND are
    probed by jumpipmitool when [n] is selected, alongside NOOP-clean
    BMNs from the plan."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    seen: list[str] = []

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        seen.append(cmd)
        if "-o json" in cmd:
            return (_MIXED_BMN_JSON, 0)
        # bmn-with-node has no CW codes → NOOP-clean (eligible for access)
        if cmd.startswith("awxstat -l mgmt bmn-with-node") or cmd.startswith(
            "awxstat -l bmc bmn-with-node"
        ):
            return ("cw_error_codes={}\n", 0)
        if cmd.startswith("jumpipmitool"):
            return ("Chassis Power is on\n", 0)
        if cmd.startswith("bmns -o wide"):
            return ("NAME    STATE  TS\nbmn  fail   1d\n", 0)
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", _capture)
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("builtins.input", lambda _p: "n")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0

    # 1. Dedicated section appears with the CW-NODE-less BMN's findings.
    assert "=== BMNs missing CW-NODE (1) ===" in out
    # workflow / workflow-step / state from labels
    for token in ("bmn-no-node", "provision-v2", "fielddiag", "fail"):
        assert token in out

    # 2. The old parenthetical "(skipped N BMN(s) ...)" wording is gone.
    assert "(skipped " not in out

    # 3. Access check probed BOTH BMNs (bmn-with-node from NOOP-clean +
    # bmn-no-node from missing-cwnode).
    ipmi_calls = [c for c in seen if c.startswith("jumpipmitool")]
    assert any("bmn-with-node" in c for c in ipmi_calls), ipmi_calls
    assert any("bmn-no-node" in c for c in ipmi_calls), ipmi_calls

    # 4. Access table contains both — and CW-NODE-less BMN reads as `(none)`.
    assert "=== Access check (NOOP + missing CW-NODE) ===" in out
    assert "(none)" in out
    assert "Checked 2 node(s)" in out


# ----------------------------- view sku --action XID-109 -------------------


_XID109_BMN_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "bmn-actionable",
                    "labels": {
                        "ds.coreweave.com/sku.cw-sku": "GPU-H100-02",
                        "ds.coreweave.com/status.asset.serial": "BMN-ACTIONABLE",
                        "flcc.coreweave.com/workflow": "orphan",
                        "flcc.coreweave.com/state": "triage",
                    },
                },
                "status": {"reportedNodeInfo": {"nodeName": "g111aaa"}},
            },
            {
                "metadata": {
                    "name": "bmn-waiting",
                    "labels": {
                        "ds.coreweave.com/sku.cw-sku": "GPU-H100-02",
                        "ds.coreweave.com/status.asset.serial": "BMN-WAITING",
                        "flcc.coreweave.com/workflow": "orphan",
                        "flcc.coreweave.com/state": "triage",
                    },
                },
                "status": {"reportedNodeInfo": {"nodeName": "g222bbb"}},
            },
        ]
    }
)


def _xid109_bmns_wide(actionable_state: str = "triage", waiting_state: str = "triage") -> str:
    return (
        "NAME             DEVICESLOT        CW-NODE   EXISTS   ONLINE   CW-SKU         "
        "BMC-IP         OWNER        CLUSTER     CWNC-STATE   WORKFLOW   "
        "RETURN-WORKFLOW   RETURN-STATE   RETURN-STEP   PREV-WORKFLOW-STEP   "
        "WORKFLOW-STEP   NEXT-WORKFLOW-STEP   PREV-STATE   STATE        "
        "NEXT-STATE   TS     ORG-ID   NODE-PROFILE\n"
        f"bmn-actionable   slot-a            g111aaa   true     true     GPU-H100-02    "
        "10.0.0.1       unassigned   tenant-x    "
        f"{actionable_state:<12} orphan     empty             empty          empty         "
        "production           empty           empty                production   triage       "
        "empty        1d     org-x    profile-y\n"
        f"bmn-waiting      slot-b            g222bbb   true     true     GPU-H100-02    "
        "10.0.0.2       unassigned   tenant-x    "
        f"{waiting_state:<12} orphan     empty             empty          empty         "
        "production           empty           empty                production   triage       "
        "empty        1d     org-x    profile-y\n"
    )


def test_main_view_sku_action_xid109_pipeline_runs_via_x_selection(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two triage BMNs both match an HO ticket mentioning XID 109. One has
    PhaseState reason=nlcc (actionable, List A), the other has reason=flcc
    (waiting, List B). Selecting `x` runs return-to-ready only on List A."""
    monkeypatch.setenv("JIRA_EMAIL", "me@x.com")
    monkeypatch.setenv("JIRA_TOKEN", "tok")

    seen: list[str] = []

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        seen.append(cmd)
        # Order matters: jsonpath check must come before the broader "-o json"
        # check since "jsonpath" is a substring of "json".
        if cmd.startswith("kubectl get bmn bmn-actionable"):
            return ("nlcc", 0)
        if cmd.startswith("kubectl get bmn bmn-waiting"):
            return ("flcc", 0)
        if "kubectl get bmns -o json" in cmd:
            return (_XID109_BMN_JSON, 0)
        if cmd.startswith("awxstat"):
            return ("cw_error_codes={}\n", 0)
        if cmd.startswith("bmns -o wide"):
            return (_xid109_bmns_wide(), 0)
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", _capture)

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )

    class _FakeJIRA:
        def __init__(self) -> None:
            pass

        def search(self, _jql: str) -> list[object]:
            from frops.jira import JIRAIssue

            return [JIRAIssue(key="HO-12345", summary="...", status="Awaiting Support")]

        def fetch_description(self, _key: str) -> str:
            return "Node moved immediately by GPUContextSwitchTimeoutXid109 condition"

        def append_to_description(self, _key: str, _block: str) -> None:
            raise AssertionError("XID-109 path should not write to JIRA")

    monkeypatch.setattr("frops.cli.JIRAClient", _FakeJIRA)
    monkeypatch.setattr("builtins.input", lambda _p: "x")

    rc = main(["view", "sku", "GPU-H100-02", "--action"])
    out = capsys.readouterr().out
    assert rc == 0

    # 1. Waiting section rendered above the plan with the right BMN.
    assert "=== XID 109 BMNs waiting for return-to-fleetops (1) ===" in out
    assert "bmn-waiting" in out
    assert "HO-12345" in out

    # 2. The actionable BMN appears in the plan under [xid-109-return-to-ready].
    assert "[xid-109-return-to-ready] 1 node(s)" in out
    assert "bmn-actionable" in out

    # 3. Selecting `x` executes return-to-ready for the actionable BMN only.
    rtr_calls = [c for c in run_calls if "return-to-ready" in c]
    assert any("return-to-ready bmn-actionable" in c for c in rtr_calls), rtr_calls
    assert not any("return-to-ready bmn-waiting" in c for c in rtr_calls), rtr_calls

    # 4. Execution summary reports the one successful action.
    assert "=== Execution summary ===" in out
    assert "1 succeeded, 0 failed" in out


def test_main_view_sku_action_runs_access_check_when_noop_selected(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picking `n` at the group-selection prompt fires jumpipmitool +
    bmns-wide for NOOP-no-codes BMNs and renders the access summary."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    seen: list[str] = []

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        seen.append(cmd)
        if "-o json" in cmd:
            return (_NOOP_BMN_JSON, 0)
        if cmd.startswith("awxstat"):
            return ("cw_error_codes={}\n", 0)
        if cmd.startswith("jumpipmitool"):
            return ("Chassis Power is on\n", 0)
        if cmd.startswith("bmns -o wide"):
            return (_fake_bmns_wide_for("ss900770x4200980", ts="10m"), 0)
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", _capture)
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0

    assert any(
        c.startswith('jumpipmitool -c "chassis power status" ss900770x4200980') for c in seen
    ), seen
    assert any(c == "bmns -o wide ss900770x4200980" for c in seen), seen

    assert "=== Access check (NOOP + missing CW-NODE) ===" in out
    assert "Checked 1 node(s): 1 reachable, 0 unreachable." in out
    for token in (
        "ss900770x4200980",
        "g826cb0",
        "provision-v2",
        "dpu-vaultify",  # workflow-step label flows through to the table
        "fail",
        "10m",
    ):
        assert token in out


def test_main_view_sku_action_skips_access_check_when_noop_not_selected(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty answer at the prompt aborts everything — including the access
    check (it's now gated by `n`, not run unconditionally)."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    seen: list[str] = []

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        seen.append(cmd)
        if "-o json" in cmd:
            return (_NOOP_BMN_JSON, 0)
        if cmd.startswith("awxstat"):
            return ("cw_error_codes={}\n", 0)
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", _capture)
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not any(c.startswith("jumpipmitool") for c in seen), seen
    assert not any(c.startswith("bmns -o wide") for c in seen), seen
    assert "=== Access check" not in out
    assert "Aborted by user" in out


def test_main_view_sku_action_skips_access_check_when_no_noop_clean_bmns(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BMN with a power-drain code (no NOOP-clean candidates) → no access pass."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)

    seen: list[str] = []

    def _capture(cmd: str, **_kwargs: object) -> tuple[str, int]:
        seen.append(cmd)
        if "-o json" in cmd:
            return (_NOOP_BMN_JSON, 0)
        if cmd.startswith("awxstat"):
            return (
                "Node:  S\nJob Status: failed\ncw_error_codes={\n  CW0211: x\n}\n",
                0,
            )
        # If the access pass were running we'd see these — assert it doesn't.
        return ("", 0)

    monkeypatch.setattr("frops.cli.capture_command", _capture)
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("builtins.input", lambda _p: "n")  # decline execution

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    # No access pass shell-outs ran.
    assert not any(c.startswith("jumpipmitool") for c in seen), seen
    assert not any(c.startswith("bmns -o wide") for c in seen), seen
    assert "=== Access check" not in out


# ----------------------------- group selection (multi-pick) ----------------


_TWO_BMN_PD_HO_JSON = json.dumps(
    {
        "items": [
            {
                "metadata": {
                    "name": "bmn-pd",
                    "labels": {"ds.coreweave.com/sku.cw-sku": "GPU-GH200-01"},
                },
                "status": {"reportedNodeInfo": {"nodeName": "g-pd"}},
            },
            {
                "metadata": {
                    "name": "bmn-ho",
                    "labels": {"ds.coreweave.com/sku.cw-sku": "GPU-GH200-01"},
                },
                "status": {"reportedNodeInfo": {"nodeName": "g-ho"}},
            },
        ]
    }
)


def _capture_two_bmn_pd_ho() -> Callable[[str], tuple[str, int]]:
    """One BMN with CW0211 (power-drain), one with CW0201 (ho-ticket)."""

    def _cap(cmd: str) -> tuple[str, int]:
        if "-o json" in cmd:
            return (_TWO_BMN_PD_HO_JSON, 0)
        if cmd.startswith("awxstat") and "bmn-pd" in cmd:
            return (
                "Job Status: failed\ncw_error_codes={\n  CW0211: x\n}\n",
                0,
            )
        if cmd.startswith("awxstat") and "bmn-ho" in cmd:
            return (
                "Job Status: failed\ncw_error_codes={\n  CW0201: y\n}\n",
                0,
            )
        return ("", 0)

    return _cap


def test_main_view_sku_action_selecting_p_only_skips_ho_ticket(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`p` at the prompt runs power-drain; ho-ticket fallback is skipped."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.setattr("frops.cli.capture_command", _capture_two_bmn_pd_ho())

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("builtins.input", lambda _p: "p")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    # Power-drain executed for bmn-pd; ho-ticket return-to-triage NOT executed.
    assert any("power-drain bmn-pd" in c for c in run_calls), run_calls
    assert not any("return-to-triage bmn-ho" in c for c in run_calls), run_calls
    assert "=== Execution summary ===" in out
    assert "1 succeeded, 0 failed" in out


def test_main_view_sku_action_selecting_p_h_combo_runs_both(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`p,h` at the prompt picks both groups."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.setattr("frops.cli.capture_command", _capture_two_bmn_pd_ho())

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("builtins.input", lambda _p: "p,h")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    assert any("power-drain bmn-pd" in c for c in run_calls), run_calls
    assert any("return-to-triage bmn-ho" in c for c in run_calls), run_calls
    assert "2 succeeded, 0 failed" in out


def test_main_view_sku_action_unknown_letters_are_silently_ignored(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Letters that don't map to an offered group are dropped; if the
    remaining selection is empty the run aborts."""
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.setattr("frops.cli.capture_command", _capture_two_bmn_pd_ho())

    run_calls: list[str] = []
    monkeypatch.setattr(
        "frops.cli.run_command",
        lambda cmd: run_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    # "z,x" — neither matches any offered letter. Result: empty selection → abort.
    monkeypatch.setattr("builtins.input", lambda _p: "z,x")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Aborted by user" in out
    # No cwctl commands beyond the original kubectl wide view.
    assert all("cwctl" not in c for c in run_calls), run_calls


def test_main_view_sku_action_yes_dry_run_mentions_execute_intent(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[str] = []
    monkeypatch.setattr(
        "frops.cli.capture_command",
        lambda cmd: captures.append(cmd) or ("", 0),  # type: ignore[func-returns-value]
    )
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    rc = main(["--dry-run", "view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert captures == [], "dry-run must not invoke kubectl/awxstat"
    assert "would also fetch JSON BMN data" in out
    assert "execute" in out
