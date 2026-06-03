"""Tests for the action classifier and plan renderer."""

from __future__ import annotations

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
    render_plan,
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
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku=sku,
        awx_reports=tuple(reports or []),
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
    assert "Totals: power-drain=2, ho-ticket=1, noop=1" in rendered


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
