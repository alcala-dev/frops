"""Tests for the action classifier and plan renderer."""

from __future__ import annotations

import pytest

from frops.action import (
    ActionKind,
    BMNTarget,
    ExecutionResult,
    ExecutionSummary,
    PlannedAction,
    actionable_actions,
    classify,
    execute_plan,
    render_execution_summary,
    render_jira_block,
    render_plan,
    resolve_ho_tickets,
)
from frops.awx import AWXReport, CWError


def _report(codes: list[tuple[str, str]] = []) -> AWXReport:  # noqa: B006 — read-only default
    return AWXReport(
        node=None,
        limit=None,
        job_id=None,
        job_name=None,
        job_url=None,
        job_status=None,
        cw_error_codes=tuple(CWError(code=c, description=d) for c, d in codes),
        raw="",
    )


def _target(
    *,
    bmn: str = "ss900770x4200980",
    cw_node: str = "g81b512",
    sku: str = "GPU-GH200-01",
    reports: list[AWXReport] | None = None,
    serial: str = "S900770X4200980",
    region: str = "RNO2",
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku=sku,
        awx_reports=tuple(reports or []),
        serial=serial,
        region=region,
    )


# --------------------------- classify ---------------------------------------


def test_classify_cw0211_on_gh200_is_power_drain() -> None:
    target = _target(reports=[_report([("CW0211", "bios missing")])])
    action = classify(target)
    assert action.kind is ActionKind.POWER_DRAIN
    assert action.triggering_codes == ("CW0211",)
    assert action.command is not None
    assert "power-drain ss900770x4200980" in action.command
    assert 'clear CW0211"' in action.command


def test_classify_cw0102_on_gh200_is_power_drain() -> None:
    target = _target(reports=[_report([("CW0102", "something")])])
    action = classify(target)
    assert action.kind is ActionKind.POWER_DRAIN
    assert action.triggering_codes == ("CW0102",)
    assert action.command is not None
    assert "clear CW0102" in action.command


def test_classify_power_drain_picks_lexicographic_first_when_both_codes() -> None:
    target = _target(reports=[_report([("CW0211", "x"), ("CW0102", "y")])])
    action = classify(target)
    assert action.kind is ActionKind.POWER_DRAIN
    assert action.triggering_codes == ("CW0102", "CW0211")
    assert action.command is not None
    assert "clear CW0102" in action.command  # sorted first wins for the message


def test_classify_cw0211_on_non_gh200_sku_is_noop() -> None:
    target = _target(
        sku="CPU-EPYC-01",
        reports=[_report([("CW0211", "x")])],
    )
    action = classify(target)
    assert action.kind is ActionKind.NOOP
    assert "not actionable for SKU CPU-EPYC-01" in action.notes


def test_classify_cw0201_is_ho_ticket_regardless_of_sku() -> None:
    target = _target(
        sku="CPU-EPYC-01",
        reports=[_report([("CW0201", "x")])],
    )
    action = classify(target)
    assert action.kind is ActionKind.HO_TICKET
    assert action.triggering_codes == ("CW0201",)
    assert action.command is not None
    assert "return-to-triage" in action.command


def test_classify_power_drain_wins_when_both_groups_present() -> None:
    target = _target(reports=[_report([("CW0211", "x"), ("CW0201", "y")])])
    action = classify(target)
    assert action.kind is ActionKind.POWER_DRAIN


def test_classify_no_codes_is_noop() -> None:
    action = classify(_target(reports=[_report([])]))
    assert action.kind is ActionKind.NOOP
    assert action.triggering_codes == ()
    assert "No CW codes detected" in action.notes


def test_classify_merges_codes_from_multiple_reports() -> None:
    mgmt = _report([("CW0211", "from mgmt")])
    bmc = _report([("CW0201", "from bmc")])
    action = classify(_target(reports=[mgmt, bmc]))
    # Power-drain should still win even though CW0201 came from BMC.
    assert action.kind is ActionKind.POWER_DRAIN
    assert action.triggering_codes == ("CW0211",)


# --------------------------- DRIVE_INSPECT classify -------------------------


def test_classify_cw0810_on_gh200_is_drive_inspect() -> None:
    target = _target(reports=[_report([("CW0810", "No drives were detected.")])])
    action = classify(target)
    assert action.kind is ActionKind.DRIVE_INSPECT
    assert action.triggering_codes == ("CW0810",)
    assert action.command is not None
    # The cwctl ticket uses serial as the device key + region from labels.
    assert f"cwctl ticket dct-action device {target.serial}" in action.command
    assert f"-r {target.region}" in action.command
    # Message references cw-node and serial for context.
    assert target.cw_node in action.command
    assert target.serial in action.command
    assert "Drives not detected" in action.command


def test_classify_cw0810_on_non_gh200_sku_is_noop() -> None:
    """DRIVE_INSPECT is GH200-only for now — other SKUs treat CW0810 as
    just another non-actionable code (NOOP)."""
    target = _target(
        sku="CPU-EPYC-01",
        reports=[_report([("CW0810", "No drives were detected.")])],
    )
    action = classify(target)
    assert action.kind is ActionKind.NOOP
    assert "not actionable for SKU CPU-EPYC-01" in action.notes


def test_classify_power_drain_wins_over_drive_inspect_when_both_present() -> None:
    """A node with both CW0211 (power-drain) and CW0810 (drives) on GH200
    should classify as power-drain — the more generic software remediation
    runs first; the drive issue persists into the next pass for ticketing."""
    target = _target(
        reports=[_report([("CW0211", "bios missing"), ("CW0810", "no drives")])],
    )
    action = classify(target)
    assert action.kind is ActionKind.POWER_DRAIN


def test_classify_drive_inspect_wins_over_ho_ticket_when_both_present() -> None:
    """A node with CW0810 (drives) and CW0201 (HO ticket) on GH200 should
    classify as drive-inspect — onsite drive work outranks administrative
    escalation when both are signaled."""
    target = _target(
        reports=[_report([("CW0810", "no drives"), ("CW0201", "ho")])],
    )
    action = classify(target)
    assert action.kind is ActionKind.DRIVE_INSPECT


# --------------------------- render_plan ------------------------------------


def test_render_plan_empty() -> None:
    rendered = render_plan([])
    assert "(no eligible BMNs found)" in rendered


def test_render_plan_groups_by_kind_and_shows_totals() -> None:
    actions = [
        classify(_target(bmn="a", reports=[_report([("CW0211", "x")])])),
        classify(_target(bmn="b", reports=[_report([("CW0201", "x")])])),
        classify(_target(bmn="c", reports=[_report([])])),
        classify(_target(bmn="d", reports=[_report([("CW0211", "x")])])),
    ]
    rendered = render_plan(actions)
    assert "[power-drain] 2 node(s)" in rendered
    assert "[ho-ticket] 1 node(s)" in rendered
    assert "[noop] 1 node(s)" in rendered
    assert (
        "Totals: power-drain=2, ho-ticket=1, xid-109-return-to-ready=0, drive-inspect=0, noop=1"
    ) in rendered


def test_render_plan_includes_command_for_actionable_kinds() -> None:
    actions = [
        PlannedAction(
            bmn="a",
            cw_node="g1",
            sku="GPU-GH200-01",
            kind=ActionKind.POWER_DRAIN,
            triggering_codes=("CW0211",),
            command="cwctl ...",
            notes="...",
        )
    ]
    rendered = render_plan(actions)
    assert "command: cwctl ..." in rendered


# --------------------------- Phase B: execute_plan --------------------------


def _action(
    *, bmn: str = "a", command: str | None = "cwctl run", kind: ActionKind = ActionKind.POWER_DRAIN
) -> PlannedAction:
    return PlannedAction(
        bmn=bmn,
        cw_node="g1",
        sku="GPU-GH200-01",
        kind=kind,
        triggering_codes=("CW0211",) if command else (),
        command=command,
        notes="",
    )


def test_actionable_actions_drops_noops() -> None:
    actions = [
        _action(bmn="a", command="cwctl 1"),
        _action(bmn="b", command=None, kind=ActionKind.NOOP),
        _action(bmn="c", command="cwctl 2"),
    ]
    actionable = actionable_actions(actions)
    assert [a.bmn for a in actionable] == ["a", "c"]


def test_execute_plan_runs_each_actionable_command_via_runner() -> None:
    actions = [
        _action(bmn="a", command="cwctl a"),
        _action(bmn="b", command="cwctl b"),
    ]
    calls: list[str] = []

    def runner(cmd: str) -> int:
        calls.append(cmd)
        return 0

    summary = execute_plan(actions, runner)
    assert calls == ["cwctl a", "cwctl b"]
    assert len(summary.results) == 2
    assert all(r.succeeded for r in summary.results)
    assert summary.worst_rc == 0


def test_execute_plan_skips_noops() -> None:
    actions = [
        _action(bmn="a", command=None, kind=ActionKind.NOOP),
        _action(bmn="b", command="cwctl b"),
    ]
    calls: list[str] = []

    summary = execute_plan(
        actions,
        lambda cmd: calls.append(cmd) or 0,  # type: ignore[func-returns-value]
    )
    assert calls == ["cwctl b"]
    assert [r.action.bmn for r in summary.results] == ["b"]


def test_execute_plan_continues_after_failure_and_returns_worst_rc() -> None:
    actions = [
        _action(bmn="a", command="cwctl a"),
        _action(bmn="b", command="cwctl b"),
        _action(bmn="c", command="cwctl c"),
    ]
    # b fails (rc=2), a and c succeed — execution should not abort on b.
    rcs = iter([0, 2, 0])
    summary = execute_plan(actions, lambda _cmd: next(rcs))
    assert [r.rc for r in summary.results] == [0, 2, 0]
    assert summary.worst_rc == 2
    assert [r.action.bmn for r in summary.failed] == ["b"]
    assert [r.action.bmn for r in summary.succeeded] == ["a", "c"]


def test_execute_plan_with_no_actions_returns_empty_summary() -> None:
    summary = execute_plan([], lambda _cmd: 0)
    assert summary.results == ()
    assert summary.worst_rc == 0
    assert summary.failed == ()


def test_render_execution_summary_empty() -> None:
    rendered = render_execution_summary(ExecutionSummary(results=()))
    assert "no actionable items were executed" in rendered


def test_render_execution_summary_lists_failures_only() -> None:
    summary = ExecutionSummary(
        results=(
            ExecutionResult(action=_action(bmn="ok"), rc=0),
            ExecutionResult(action=_action(bmn="bad", command="cwctl bad"), rc=2),
        )
    )
    rendered = render_execution_summary(summary)
    assert "Ran 2 action(s): 1 succeeded, 1 failed." in rendered
    assert "bad" in rendered
    assert "exit 2" in rendered
    assert "command: cwctl bad" in rendered
    # Successful BMN shouldn't be listed individually.
    assert "  - ok " not in rendered


# --------------------------- HO ticket resolution ---------------------------


def _ho_action(
    *, bmn: str = "ss900770x4200980", cw_node: str = "g81b512", sku: str = "GPU-GH200-01"
) -> PlannedAction:
    """An HO_TICKET PlannedAction with the cwctl fallback command set."""
    return PlannedAction(
        bmn=bmn,
        cw_node=cw_node,
        sku=sku,
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=f"cwctl flcc node -w return-to-triage {bmn} -o -m '...'",
        notes="fallback",
    )


def _ho_target(
    *,
    bmn: str = "ss900770x4200980",
    cw_node: str = "g81b512",
    sku: str = "GPU-GH200-01",
    serial: str = "S900770X4200980",
) -> BMNTarget:
    return BMNTarget(bmn=bmn, cw_node=cw_node, sku=sku, awx_reports=(), serial=serial)


def test_bmn_target_search_identifiers_dedups_and_skips_empty() -> None:
    t = _ho_target()
    assert t.search_identifiers == ("ss900770x4200980", "g81b512", "S900770X4200980")
    # When serial is empty, drops it. When bmn equals cw_node, dedups.
    t2 = BMNTarget(bmn="x", cw_node="x", sku="s", awx_reports=(), serial="")
    assert t2.search_identifiers == ("x",)


def test_resolve_ho_tickets_replaces_command_with_jira_issue_on_match() -> None:
    action = _ho_action()
    target = _ho_target()
    seen: list[tuple[str, ...]] = []

    def _search(ids: tuple[str, ...]) -> str | None:
        seen.append(ids)
        return "HO-12345"

    resolved = resolve_ho_tickets([action], _search, {action.bmn: target})
    assert seen == [target.search_identifiers]
    (got,) = resolved
    assert got.jira_issue == "HO-12345"
    assert got.command is None
    assert got.kind is ActionKind.HO_TICKET
    assert "HO-12345" in got.notes


def test_resolve_ho_tickets_keeps_cwctl_fallback_when_no_match() -> None:
    action = _ho_action()
    target = _ho_target()
    resolved = resolve_ho_tickets([action], lambda _ids: None, {action.bmn: target})
    (got,) = resolved
    assert got.jira_issue is None
    assert got.command == action.command  # unchanged
    assert got.notes == action.notes


def test_resolve_ho_tickets_leaves_non_ho_actions_alone() -> None:
    power = _action(bmn="pd", command="cwctl pd", kind=ActionKind.POWER_DRAIN)
    ho = _ho_action(bmn="ho")
    noop = _action(bmn="np", command=None, kind=ActionKind.NOOP)

    def _search(_ids: tuple[str, ...]) -> str | None:
        return "HO-9"

    resolved = resolve_ho_tickets(
        [power, ho, noop],
        _search,
        {"ho": _ho_target(bmn="ho")},
    )
    # Power-drain and noop are untouched; only HO action is resolved.
    assert resolved[0] is power
    assert resolved[1].jira_issue == "HO-9"
    assert resolved[2] is noop


def test_resolve_ho_tickets_falls_back_to_bmn_when_target_missing() -> None:
    action = _ho_action()
    seen: list[tuple[str, ...]] = []

    def _search(ids: tuple[str, ...]) -> str | None:
        seen.append(ids)
        return None

    # No matching target in the map — should use (bmn,) so we still search.
    resolve_ho_tickets([action], _search, {})
    assert seen == [(action.bmn,)]


# --------------------------- actionable_actions w/ JIRA ---------------------


def test_actionable_actions_includes_jira_resolved_actions() -> None:
    shell_action = _action(bmn="sh", command="cwctl x")
    jira_action = PlannedAction(
        bmn="ji",
        cw_node="g1",
        sku="s",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    noop_action = _action(bmn="np", command=None, kind=ActionKind.NOOP)
    result = actionable_actions([shell_action, jira_action, noop_action])
    assert [a.bmn for a in result] == ["sh", "ji"]


# --------------------------- execute_plan JIRA dispatch ---------------------


def test_execute_plan_routes_jira_actions_to_jira_runner() -> None:
    shell_action = _action(bmn="sh", command="cwctl x")
    jira_action = PlannedAction(
        bmn="ji",
        cw_node="g1",
        sku="s",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    shell_calls: list[str] = []
    jira_calls: list[PlannedAction] = []

    summary = execute_plan(
        [shell_action, jira_action],
        runner=lambda cmd: shell_calls.append(cmd) or 0,  # type: ignore[func-returns-value]
        jira_runner=lambda act: jira_calls.append(act) or 0,  # type: ignore[func-returns-value]
    )
    assert shell_calls == ["cwctl x"]
    assert [a.bmn for a in jira_calls] == ["ji"]
    assert summary.worst_rc == 0


def test_execute_plan_raises_when_jira_action_has_no_jira_runner() -> None:
    jira_action = PlannedAction(
        bmn="ji",
        cw_node="g1",
        sku="s",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    with pytest.raises(ValueError, match="no jira_runner"):
        execute_plan([jira_action], lambda _cmd: 0, jira_runner=None)


def test_execute_plan_jira_failure_propagates_via_worst_rc() -> None:
    jira_action = PlannedAction(
        bmn="ji",
        cw_node="g1",
        sku="s",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    summary = execute_plan([jira_action], lambda _cmd: 0, jira_runner=lambda _act: 3)
    assert summary.worst_rc == 3
    assert [a.bmn for a in (r.action for r in summary.failed)] == ["ji"]


# --------------------------- render_plan & jira_block -----------------------


def test_render_plan_shows_jira_target_instead_of_command_when_resolved() -> None:
    jira_action = PlannedAction(
        bmn="ji",
        cw_node="g1",
        sku="GPU-GH200-01",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=None,
        notes="found ticket",
        jira_issue="HO-77",
    )
    rendered = render_plan([jira_action])
    assert "jira: append to HO-77 description" in rendered
    # The cwctl line for HO_TICKET should not be rendered when resolved.
    assert "command:" not in rendered


def test_render_jira_block_contains_expected_fields() -> None:
    action = PlannedAction(
        bmn="ss900770x4200980",
        cw_node="g81b512",
        sku="GPU-GH200-01",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201", "CW0204"),
        command=None,
        notes="found ticket HO-77",
        jira_issue="HO-77",
    )
    block = render_jira_block(action, "2026-06-02T18:42:00Z")
    assert "2026-06-02T18:42:00Z" in block
    assert "ss900770x4200980" in block
    assert "g81b512" in block
    assert "GPU-GH200-01" in block
    assert "CW0201, CW0204" in block
    assert "found ticket HO-77" in block


def test_render_execution_summary_shows_jira_target_on_failure() -> None:
    jira_action = PlannedAction(
        bmn="ji",
        cw_node="g1",
        sku="s",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    summary = ExecutionSummary(
        results=(ExecutionResult(action=jira_action, rc=1),),
    )
    rendered = render_execution_summary(summary)
    assert "exit 1" in rendered
    assert "append to HO-42" in rendered
    assert "command:" not in rendered
