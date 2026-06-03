"""Tests for the access-check pass over NOOP-no-codes BMNs."""

from __future__ import annotations

from frops.access import (
    AccessReport,
    _extract_ts,
    _first_useful_line,
    access_check_targets,
    check_access,
    check_all,
    render_access_summary,
    render_missing_cwnode_summary,
)
from frops.action import ActionKind, BMNTarget, PlannedAction

# --------------------------- helpers ----------------------------------------


def _target(
    *,
    bmn: str = "ss900770x4200980",
    cw_node: str = "g81b512",
    workflow: str = "broken-collect",
    workflow_step: str = "collect",
    state: str = "fail",
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku="GPU-GH200-01",
        awx_reports=(),
        serial="",
        workflow=workflow,
        workflow_step=workflow_step,
        state=state,
    )


def _action(
    *,
    bmn: str = "ss900770x4200980",
    kind: ActionKind = ActionKind.NOOP,
    triggering_codes: tuple[str, ...] = (),
    command: str | None = None,
) -> PlannedAction:
    return PlannedAction(
        bmn=bmn,
        cw_node="g81b512",
        sku="GPU-GH200-01",
        kind=kind,
        triggering_codes=triggering_codes,
        command=command,
        notes="",
    )


# Realistic single-BMN `bmns -o wide` output that mirrors what the user pasted.
_BMNS_WIDE_OUTPUT = (
    "NAME               DEVICESLOT        CW-NODE   EXISTS   ONLINE   "
    "CW-SKU         BMC-IP          OWNER        CLUSTER                       "
    "CWNC-STATE   WORKFLOW       RETURN-WORKFLOW   RETURN-STATE   RETURN-STEP   "
    "PREV-WORKFLOW-STEP   WORKFLOW-STEP   NEXT-WORKFLOW-STEP   PREV-STATE     "
    "STATE   NEXT-STATE   TS    ORG-ID        NODE-PROFILE\n"
    "ss900770x4120327   s6-r106-node-05   g826cb0   true     false    "
    "GPU-GH200-01   10.168.17.133   unassigned   tenant-cw-internal-fleetops   "
    "triage       provision-v2   empty             empty          empty         "
    "node-vaultify        dpu-vaultify    dpu-zap              dpu-vaultify   "
    "fail    empty        10m   cw-internal   tnt-cw-internal-fleetops-default-gpu-gh200-rno2\n"
)


# --------------------------- access_check_targets ---------------------------


def test_access_check_targets_picks_noop_without_codes_only() -> None:
    actions = [
        _action(bmn="noop-clean", kind=ActionKind.NOOP, triggering_codes=()),
        _action(bmn="noop-coded", kind=ActionKind.NOOP, triggering_codes=("CW0999",)),
        _action(bmn="power", kind=ActionKind.POWER_DRAIN, command="cwctl"),
        _action(bmn="ho", kind=ActionKind.HO_TICKET, command="cwctl"),
    ]
    targets = {a.bmn: _target(bmn=a.bmn) for a in actions}
    selected = access_check_targets(actions, targets)
    assert [t.bmn for t in selected] == ["noop-clean"]


def test_access_check_targets_skips_unknown_bmns() -> None:
    actions = [_action(bmn="orphan", kind=ActionKind.NOOP, triggering_codes=())]
    assert access_check_targets(actions, targets_by_bmn={}) == []


# --------------------------- _extract_ts (parser) ---------------------------


def test_extract_ts_pulls_value_from_canonical_bmns_wide_output() -> None:
    assert _extract_ts(_BMNS_WIDE_OUTPUT) == "10m"


def test_extract_ts_handles_long_state_values_that_would_break_position_slicing() -> None:
    # STATE column is the cell BEFORE the rendered TS in the header order; if
    # parsing used fixed header positions, a long state value would push TS
    # right and the slice would land in NEXT-STATE.
    overflow = (
        "NAME   STATE                   TS    ORG-ID\n"
        "bmn-1  production-reboot-fail  2h    cw-internal\n"
    )
    assert _extract_ts(overflow) == "2h"


def test_extract_ts_returns_question_mark_when_no_ts_header() -> None:
    assert _extract_ts("NAME  STATE\nbmn-1 fail\n") == "?"


def test_extract_ts_returns_question_mark_on_empty_output() -> None:
    assert _extract_ts("") == "?"
    assert _extract_ts("only-header\n") == "?"


# --------------------------- _first_useful_line -----------------------------


def test_first_useful_line_strips_and_picks_first_nonblank() -> None:
    assert _first_useful_line("\n  \nChassis Power is on\nfoo\n") == "Chassis Power is on"


def test_first_useful_line_empty() -> None:
    assert _first_useful_line("") == ""
    assert _first_useful_line("\n\n  \n") == ""


def test_first_useful_line_caps_length() -> None:
    long = "x" * 500
    assert len(_first_useful_line(long)) == 200


# --------------------------- check_access -----------------------------------


def test_check_access_marks_reachable_on_zero_exit() -> None:
    target = _target()

    def _capture(cmd: str) -> tuple[str, int]:
        if cmd.startswith("jumpipmitool"):
            return ("Chassis Power is on\n", 0)
        return (_BMNS_WIDE_OUTPUT, 0)

    report = check_access(target, _capture)
    assert report.reachable is True
    assert report.detail == "Chassis Power is on"
    assert report.ts == "10m"
    assert report.workflow == "broken-collect"
    assert report.workflow_step == "collect"
    assert report.state == "fail"
    assert report.bmn == target.bmn


def test_check_access_marks_unreachable_on_non_zero_exit() -> None:
    target = _target(bmn="dead-bmn")
    error_text = "Get Chassis Power Status failed: Unable to establish IPMI v2 / RMCP+ session\n"

    def _capture(cmd: str) -> tuple[str, int]:
        if cmd.startswith("jumpipmitool"):
            return (error_text, 1)
        return (_BMNS_WIDE_OUTPUT, 0)

    report = check_access(target, _capture)
    assert report.reachable is False
    assert "RMCP+" in report.detail


def test_check_access_substitutes_unknown_for_empty_workflow_state() -> None:
    target = _target(workflow="", workflow_step="", state="")

    def _capture(_cmd: str) -> tuple[str, int]:
        return ("Chassis Power is on\n", 0)

    report = check_access(target, _capture)
    assert report.workflow == "(unknown)"
    assert report.workflow_step == "(unknown)"
    assert report.state == "(unknown)"


def test_check_access_falls_back_to_exit_code_when_no_output() -> None:
    target = _target()

    def _capture(cmd: str) -> tuple[str, int]:
        if cmd.startswith("jumpipmitool"):
            return ("", 7)
        return (_BMNS_WIDE_OUTPUT, 0)

    report = check_access(target, _capture)
    assert report.reachable is False
    assert report.detail == "exit 7"


# --------------------------- check_all --------------------------------------


def test_check_all_runs_each_target_and_sorts_by_bmn() -> None:
    targets = [_target(bmn=name) for name in ("zeta", "alpha", "mu")]
    seen: list[str] = []

    def _capture(cmd: str) -> tuple[str, int]:
        seen.append(cmd)
        if cmd.startswith("jumpipmitool"):
            return ("Chassis Power is on\n", 0)
        return (_BMNS_WIDE_OUTPUT, 0)

    reports = check_all(targets, _capture, max_workers=2)
    assert [r.bmn for r in reports] == ["alpha", "mu", "zeta"]
    # Each target gets 2 captures (ipmi + bmns); 3 targets * 2 = 6.
    assert len(seen) == 6


def test_check_all_empty_list_skips_executor() -> None:
    calls: list[str] = []
    assert check_all([], lambda c: calls.append(c) or ("", 0)) == []  # type: ignore[func-returns-value]
    assert calls == []


# --------------------------- render_access_summary --------------------------


def test_render_access_summary_empty_returns_empty_string() -> None:
    assert render_access_summary([]) == ""


def test_render_access_summary_shows_header_table_and_counts() -> None:
    reports = [
        AccessReport(
            bmn="bmn-a",
            cw_node="g-a",
            workflow="provision-v2",
            workflow_step="dpu-vaultify",
            state="fail",
            ts="10m",
            reachable=True,
            detail="Chassis Power is on",
        ),
        AccessReport(
            bmn="bmn-b",
            cw_node="g-b",
            workflow="broken-collect",
            workflow_step="collect",
            state="fail",
            ts="2h",
            reachable=False,
            detail="RMCP+ session timeout",
        ),
    ]
    rendered = render_access_summary(reports)
    assert "=== Access check (NOOP + missing CW-NODE) ===" in rendered
    assert "Checked 2 node(s): 1 reachable, 1 unreachable." in rendered
    # Header tokens present
    for token in ("BMN", "CW-NODE", "WORKFLOW", "WORKFLOW-STEP", "STATE", "TS", "REACH"):
        assert token in rendered
    # Data values present (incl. new workflow-step value)
    for value in ("bmn-a", "g-a", "provision-v2", "dpu-vaultify", "fail", "10m"):
        assert value in rendered
    assert "collect" in rendered  # second row's workflow-step
    # Unreachable detail surfaces; clean "Chassis Power is on" does NOT show
    # a redundant detail line (only odd output gets the ↳ annotation).
    assert "RMCP+ session timeout" in rendered
    lines = rendered.splitlines()
    bmn_a_idx = next(i for i, line in enumerate(lines) if line.startswith("bmn-a"))
    assert not lines[bmn_a_idx + 1].lstrip().startswith("↳")


def test_render_access_summary_shows_detail_for_chassis_power_off() -> None:
    # `off` is a non-"Chassis Power is on" string → detail surfaces too, so
    # operators see at a glance which nodes are off vs unreachable.
    reports = [
        AccessReport(
            bmn="bmn-off",
            cw_node="g-off",
            workflow="wf",
            workflow_step="step",
            state="fail",
            ts="1d",
            reachable=True,
            detail="Chassis Power is off",
        ),
    ]
    rendered = render_access_summary(reports)
    # "off" is a known value — should NOT trigger the detail annotation.
    assert "↳ Chassis Power is off" not in rendered


def test_render_access_summary_shows_none_for_empty_cw_node() -> None:
    """CW-NODE-less BMNs are included in the same access table; their
    CW-NODE column reads `(none)` so they're unambiguous."""
    reports = [
        AccessReport(
            bmn="bmn-stub",
            cw_node="",
            workflow="provision-v2",
            workflow_step="fielddiag",
            state="fail",
            ts="1d",
            reachable=False,
            detail="exit 1",
        ),
    ]
    rendered = render_access_summary(reports)
    assert "(none)" in rendered
    # bmn-stub row should be unreachable
    assert "no" in rendered
    assert "Checked 1 node(s): 0 reachable, 1 unreachable." in rendered


# --------------------------- render_missing_cwnode_summary ------------------


def test_render_missing_cwnode_summary_empty_returns_empty_string() -> None:
    assert render_missing_cwnode_summary([]) == ""


def test_render_missing_cwnode_summary_renders_table_with_findings() -> None:
    targets = [
        BMNTarget(
            bmn="ss900770x4113539",
            cw_node="",
            sku="GPU-GH200-01",
            awx_reports=(),
            workflow="provision-v2",
            workflow_step="fielddiag",
            state="fail",
        ),
        BMNTarget(
            bmn="ss900770x4202580",
            cw_node="",
            sku="GPU-GH200-01",
            awx_reports=(),
            workflow="",  # missing label
            workflow_step="",
            state="",
        ),
    ]
    rendered = render_missing_cwnode_summary(targets)
    assert "=== BMNs missing CW-NODE (2) ===" in rendered
    # Header tokens
    for token in ("BMN", "WORKFLOW", "WORKFLOW-STEP", "STATE"):
        assert token in rendered
    # First BMN's known values
    for value in ("ss900770x4113539", "provision-v2", "fielddiag", "fail"):
        assert value in rendered
    # Missing labels fall back to "(unknown)" so the row is never blank
    assert "(unknown)" in rendered
    assert "ss900770x4202580" in rendered
