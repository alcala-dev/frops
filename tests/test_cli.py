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

    def _capture(cmd: str) -> tuple[str, int]:
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


def test_main_view_sku_action_without_yes_prompts_and_aborts_on_no(
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
    monkeypatch.setattr("frops.cli.run_command", lambda _cmd: 0)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    # Plan rendered…
    assert "=== Planned actions ===" in out
    assert "[power-drain] 1 node(s)" in out
    # …user said no → execution skipped, no execution summary printed.
    assert "Aborted by user" in out
    assert "=== Execution summary ===" not in out


def test_main_view_sku_action_without_yes_executes_on_y(
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

    def _run(cmd: str) -> int:
        run_calls.append(cmd)
        return 0

    monkeypatch.setattr("frops.cli.run_command", _run)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    rc = main(["view", "sku", "GPU-GH200-01", "--action"])
    out = capsys.readouterr().out
    assert rc == 0
    # First run_command call is the wide kubectl view; cwctl calls follow.
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

    def _capture(cmd: str) -> tuple[str, int]:
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


def test_main_view_sku_action_yes_with_only_noops_skips_execution(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No CW codes in either awxstat output → classifier returns NOOP.
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
    # Input must not be called even without --yes when there's nothing actionable.
    monkeypatch.setattr("builtins.input", _input_should_not_be_called)

    rc = main(["view", "sku", "GPU-GH200-01", "--action", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    # Only the wide kubectl view runs — no cwctl follow-ups.
    assert len(run_calls) == 1
    assert "Nothing actionable to execute" in out


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
