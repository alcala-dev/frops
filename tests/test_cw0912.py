"""Tests for the CW0912 stage-1 follow-up (node-zap rerun scheduling)."""

from __future__ import annotations

from frops.action import (
    ActionKind,
    BMNTarget,
    ExecutionResult,
    ExecutionSummary,
    PlannedAction,
)
from frops.cw0912 import (
    CW0912,
    NODE_ZAP_DELAY,
    CW0912Rerun,
    node_zap_rerun_command,
    render_node_zap_rerun_summary,
    schedule_node_zap_reruns,
)


def _target(*, bmn: str = "bmn-1", workflow: str = "return-to-prod") -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=f"g-{bmn}",
        sku="GPU-GH200-01",
        awx_reports=(),
        serial=f"SN-{bmn}",
        workflow=workflow,
        region="RNO2A",
    )


def _result(
    *,
    bmn: str,
    kind: ActionKind = ActionKind.POWER_DRAIN,
    codes: tuple[str, ...] = (CW0912,),
    rc: int = 0,
) -> ExecutionResult:
    action = PlannedAction(
        bmn=bmn,
        cw_node=f"g-{bmn}",
        sku="GPU-GH200-01",
        kind=kind,
        triggering_codes=codes,
        command=f"cwctl ... {bmn}",
        notes="",
    )
    return ExecutionResult(action=action, rc=rc)


# --------------------------- node_zap_rerun_command --------------------------


def test_node_zap_rerun_command_renders_workflow_and_step() -> None:
    cmd = node_zap_rerun_command(bmn="ss900770x4123", workflow="return-to-prod")
    assert "cwctl flcc node -w return-to-prod -s node-zap ss900770x4123" in cmd
    assert '-m "rerunning node-zap after remote power-drain"' in cmd


# --------------------------- schedule_node_zap_reruns -----------------------


def test_schedule_skips_non_cw0912_power_drains() -> None:
    summary = ExecutionSummary(
        results=(
            _result(bmn="cw0211-only", codes=("CW0211",)),
            _result(bmn="cw0102-only", codes=("CW0102",)),
        )
    )
    targets = {"cw0211-only": _target(bmn="cw0211-only"), "cw0102-only": _target(bmn="cw0102-only")}
    calls: list[tuple[str, str]] = []

    def _schedule(cmd: str, when: str) -> tuple[bool, str]:
        calls.append((cmd, when))
        return True, "job 1 at <time>"

    reruns = schedule_node_zap_reruns(summary, targets, schedule=_schedule)
    assert reruns == []
    assert calls == []


def test_schedule_skips_failed_power_drains() -> None:
    summary = ExecutionSummary(results=(_result(bmn="failed", rc=1),))
    targets = {"failed": _target(bmn="failed")}
    calls: list[tuple[str, str]] = []

    def _schedule(cmd: str, when: str) -> tuple[bool, str]:
        calls.append((cmd, when))
        return True, "job 1 at <time>"

    reruns = schedule_node_zap_reruns(summary, targets, schedule=_schedule)
    assert reruns == []
    assert calls == []


def test_schedule_records_success_for_cw0912_power_drain() -> None:
    summary = ExecutionSummary(results=(_result(bmn="ok"),))
    targets = {"ok": _target(bmn="ok", workflow="return-to-prod")}

    def _schedule(cmd: str, when: str) -> tuple[bool, str]:
        assert when == NODE_ZAP_DELAY
        assert "cwctl flcc node -w return-to-prod -s node-zap ok" in cmd
        return True, "job 17 at Wed Jun 10 21:30:00 2026"

    (rerun,) = schedule_node_zap_reruns(summary, targets, schedule=_schedule)
    assert rerun.bmn == "ok"
    assert rerun.scheduled is True
    assert "job 17" in rerun.detail
    assert "node-zap ok" in rerun.command


def test_schedule_records_failure_when_workflow_label_missing() -> None:
    summary = ExecutionSummary(results=(_result(bmn="no-wf"),))
    targets = {"no-wf": _target(bmn="no-wf", workflow="")}

    def _schedule(*_a: str) -> tuple[bool, str]:
        raise AssertionError("schedule should NOT be invoked when workflow is missing")

    (rerun,) = schedule_node_zap_reruns(summary, targets, schedule=_schedule)
    assert rerun.scheduled is False
    assert "workflow" in rerun.detail.lower()
    assert rerun.command == ""  # no command was renderable


def test_schedule_records_at_failure_with_detail() -> None:
    summary = ExecutionSummary(results=(_result(bmn="at-fail"),))
    targets = {"at-fail": _target(bmn="at-fail")}

    def _schedule(_cmd: str, _when: str) -> tuple[bool, str]:
        return False, "at(1) returned 1: atd not running"

    (rerun,) = schedule_node_zap_reruns(summary, targets, schedule=_schedule)
    assert rerun.scheduled is False
    assert "atd not running" in rerun.detail
    # Command is still populated so the rendered summary can show it for
    # manual execution.
    assert "node-zap at-fail" in rerun.command


def test_schedule_handles_target_missing_from_lookup() -> None:
    # Defensive: BMN executed but not present in targets_by_bmn (would
    # only happen if state-machine plumbing changes). We surface a
    # failure record rather than crashing.
    summary = ExecutionSummary(results=(_result(bmn="ghost"),))
    targets: dict[str, BMNTarget] = {}

    (rerun,) = schedule_node_zap_reruns(summary, targets, schedule=lambda *_a: (True, ""))
    assert rerun.scheduled is False
    assert rerun.command == ""


# --------------------------- render_node_zap_rerun_summary -------------------


def test_render_empty_input_returns_empty_string() -> None:
    assert render_node_zap_rerun_summary([]) == ""


def test_render_shows_status_table_with_session_warning() -> None:
    reruns = [
        CW0912Rerun(
            bmn="ok-bmn",
            cw_node="g-ok",
            command="cwctl flcc node -w return-to-prod -s node-zap ok-bmn ...",
            scheduled=True,
            detail="job 12 at Wed Jun 10 21:30:00 2026",
        ),
        CW0912Rerun(
            bmn="fail-bmn",
            cw_node="g-fail",
            command="cwctl flcc node -w return-to-prod -s node-zap fail-bmn ...",
            scheduled=False,
            detail="`at` command not found on this host",
        ),
    ]
    rendered = render_node_zap_rerun_summary(reruns)
    assert "=== CW0912 node-zap reruns scheduled (2) ===" in rendered
    # Operator-facing reminder about session/auth survival (across a line break).
    assert "tsh/Doppler" in rendered
    assert "cwctl auth must still" in rendered
    assert "ok-bmn" in rendered
    assert "fail-bmn" in rendered
    # Both statuses surface; failure copy includes the cwctl command so
    # the operator can run it manually.
    assert "scheduled" in rendered
    assert "FAILED" in rendered
    assert "node-zap fail-bmn" in rendered
