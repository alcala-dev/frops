"""Tests for the action classifier and plan renderer."""

from __future__ import annotations

from frops.action import (
    ActionKind,
    BMNTarget,
    PlannedAction,
    classify,
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
