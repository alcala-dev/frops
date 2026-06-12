"""Tests for the CW0912 stage-2 state machine + override pipeline."""

from __future__ import annotations

from frops.action import ActionKind, BMNTarget, PlannedAction
from frops.awx import AWXReport, CWError
from frops.cw0912_remediation import (
    CW0912,
    GH200_SKU,
    CW0912Candidate,
    CW0912Stage,
    apply_cw0912_overrides,
    build_tray_reseat_search_jql,
    clear_recovered_cw0912_states,
    collect_cw0912_candidates,
    persist_cw0912_state_after_execute,
    render_cw0912_skipped_summary,
    render_rma_template,
    resolve_cw0912_rma_tickets,
    rma_return_to_triage_command,
    tray_reseat_command,
)
from frops.cw0912_state import (
    STAGE_POWER_DRAIN_SCHEDULED,
    STAGE_RMA_ESCALATED,
    STAGE_TRAY_RESEAT_FILED,
    CW0912State,
)


def _report(
    *,
    job_id: str,
    codes: tuple[tuple[str, str], ...] = ((CW0912, "GPU link timeout"),),
) -> AWXReport:
    return AWXReport(
        node="S1",
        limit="10.0.0.1",
        job_id=job_id,
        job_name="fwmanager",
        job_url="https://awx/...",
        job_status="failed",
        cw_error_codes=tuple(CWError(code=c, description=d) for c, d in codes),
        raw="",
    )


def _target(
    *,
    bmn: str = "ss900770x4123",
    cw_node: str = "g75cb28",
    serial: str = "S900770X4123",
    region: str = "RNO2A",
    sku: str = GH200_SKU,
    job_id: str = "320078",
    codes: tuple[tuple[str, str], ...] = ((CW0912, "GPU link timeout"),),
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku=sku,
        awx_reports=(_report(job_id=job_id, codes=codes),),
        serial=serial,
        region=region,
    )


def _power_drain_action(
    bmn: str = "ss900770x4123", codes: tuple[str, ...] = (CW0912,)
) -> PlannedAction:
    return PlannedAction(
        bmn=bmn,
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.POWER_DRAIN,
        triggering_codes=codes,
        command=f"cwctl flcc node --one-off -w orphan -s power-drain {bmn} ...",
        notes="",
    )


# --------------------------- tray_reseat_command ----------------------------


def test_tray_reseat_command_uses_documented_form() -> None:
    cmd = tray_reseat_command(bmn="bmn-1", cw_node="g1", serial="S1", region="RNO2A")
    assert "cwctl ticket dct-action device bmn-1" in cmd
    assert "Please reseat the GPU tray on node (g1 | SN: S1)" in cmd
    assert "-r RNO2A" in cmd


# --------------------------- build_tray_reseat_search_jql -------------------


def test_jql_quotes_DO_and_includes_tray_keywords() -> None:
    jql = build_tray_reseat_search_jql(("ss900770x4123", "g75cb28"))
    assert jql is not None
    # `DO` is a reserved JQL word.
    assert 'project = "DO"' in jql
    for kw in ("gpu tray", "tray reseat", "reseat the gpu"):
        assert kw in jql


def test_jql_returns_none_when_no_identifiers() -> None:
    assert build_tray_reseat_search_jql(()) is None
    assert build_tray_reseat_search_jql(("", "")) is None


# --------------------------- collect_cw0912_candidates ----------------------


def test_collect_skips_non_gh200_skus() -> None:
    target = _target(sku="GPU-H100-02")
    candidates = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: None,
        do_search=lambda _ids: (None, None),
    )
    assert candidates == []


def test_collect_skips_when_cw0912_absent_from_current_job() -> None:
    target = _target(codes=(("CW0211", "x"),))
    candidates = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: None,
        do_search=lambda _ids: (None, None),
    )
    assert candidates == []


def test_collect_first_occurrence_when_no_prior_state() -> None:
    target = _target()
    (cand,) = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: None,
        do_search=lambda _ids: (None, None),
    )
    assert cand.stage is CW0912Stage.FIRST_OCCURRENCE
    assert cand.prior_state is None
    assert cand.current_job_id == "320078"


def test_collect_in_progress_when_job_id_matches_prior() -> None:
    target = _target(job_id="320078")
    prior = CW0912State(
        bmn=target.bmn,
        job_id="320078",
        observed_at="2026-06-11T17:00:00Z",
        stage=STAGE_POWER_DRAIN_SCHEDULED,
    )
    do_calls: list[tuple[str, ...]] = []

    def _do_search(ids: tuple[str, ...]) -> tuple[str | None, str | None]:
        do_calls.append(ids)
        return None, None

    (cand,) = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: prior,
        do_search=_do_search,
    )
    assert cand.stage is CW0912Stage.IN_PROGRESS
    # IN_PROGRESS doesn't need DO dedup — JIRA shouldn't be queried.
    assert do_calls == []


def test_collect_second_occurrence_when_new_job_after_power_drain() -> None:
    target = _target(job_id="320200")
    prior = CW0912State(
        bmn=target.bmn,
        job_id="320078",  # different
        observed_at="2026-06-11T17:00:00Z",
        stage=STAGE_POWER_DRAIN_SCHEDULED,
    )
    (cand,) = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: prior,
        do_search=lambda _ids: (None, None),
    )
    assert cand.stage is CW0912Stage.SECOND_OCCURRENCE
    assert cand.actionable is True
    assert cand.prior_state == prior


def test_collect_second_occurrence_skipped_when_dedup_hit() -> None:
    target = _target(job_id="320200")
    prior = CW0912State(
        bmn=target.bmn,
        job_id="320078",
        observed_at="2026-06-11T17:00:00Z",
        stage=STAGE_POWER_DRAIN_SCHEDULED,
    )
    (cand,) = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: prior,
        do_search=lambda _ids: ("DO-99", None),
    )
    assert cand.stage is CW0912Stage.SECOND_OCCURRENCE
    assert cand.existing_do_ticket == "DO-99"
    assert cand.actionable is False  # dedup blocked


def test_collect_third_occurrence_after_tray_reseat_filed() -> None:
    target = _target(job_id="320300")
    prior = CW0912State(
        bmn=target.bmn,
        job_id="320200",
        observed_at="2026-06-11T17:00:00Z",
        stage=STAGE_TRAY_RESEAT_FILED,
    )
    (cand,) = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: prior,
        do_search=lambda _ids: (None, None),
    )
    assert cand.stage is CW0912Stage.THIRD_OCCURRENCE
    assert cand.actionable is True  # phase 3 emits CW0912_RMA_ESCALATE


def test_collect_corrupt_prior_stage_falls_back_to_first_occurrence() -> None:
    # Defensive: if the state file somehow has a junk `stage` value, we
    # shouldn't crash — re-run stage 1 (safe baseline).
    target = _target(job_id="320200")
    prior = CW0912State(
        bmn=target.bmn,
        job_id="320078",
        observed_at="2026-06-11T17:00:00Z",
        stage="some_unknown_stage",
    )
    (cand,) = collect_cw0912_candidates(
        [target],
        state_reader=lambda _b: prior,
        do_search=lambda _ids: (None, None),
    )
    assert cand.stage is CW0912Stage.FIRST_OCCURRENCE


# --------------------------- apply_cw0912_overrides -------------------------


def _candidate(
    *,
    stage: CW0912Stage,
    bmn: str = "ss900770x4123",
    existing_do_ticket: str | None = None,
    do_search_error: str | None = None,
    prior_state: CW0912State | None = None,
) -> CW0912Candidate:
    return CW0912Candidate(
        bmn=bmn,
        cw_node="g75cb28",
        sku=GH200_SKU,
        serial="S900770X4123",
        region="RNO2A",
        current_job_id="320200",
        stage=stage,
        prior_state=prior_state,
        existing_do_ticket=existing_do_ticket,
        do_search_error=do_search_error,
    )


def test_apply_leaves_first_occurrence_untouched() -> None:
    actions = [_power_drain_action()]
    cands = [_candidate(stage=CW0912Stage.FIRST_OCCURRENCE)]
    out = apply_cw0912_overrides(actions, cands)
    assert out == actions  # POWER_DRAIN preserved verbatim


def test_apply_downgrades_in_progress_to_noop() -> None:
    actions = [_power_drain_action()]
    cands = [_candidate(stage=CW0912Stage.IN_PROGRESS)]
    (rewritten,) = apply_cw0912_overrides(actions, cands)
    assert rewritten.kind is ActionKind.NOOP
    assert rewritten.command is None
    assert "wait for the scheduled node-zap rerun" in rewritten.notes


def test_apply_rewrites_actionable_second_occurrence_to_tray_reseat() -> None:
    actions = [_power_drain_action()]
    cands = [_candidate(stage=CW0912Stage.SECOND_OCCURRENCE)]
    (rewritten,) = apply_cw0912_overrides(actions, cands)
    assert rewritten.kind is ActionKind.CW0912_TRAY_RESEAT
    assert rewritten.command is not None
    assert "Please reseat the GPU tray" in rewritten.command
    assert "ss900770x4123" in rewritten.command


def test_apply_downgrades_second_occurrence_with_dedup_hit_to_noop() -> None:
    actions = [_power_drain_action()]
    cands = [
        _candidate(
            stage=CW0912Stage.SECOND_OCCURRENCE,
            existing_do_ticket="DO-99",
        )
    ]
    (rewritten,) = apply_cw0912_overrides(actions, cands)
    assert rewritten.kind is ActionKind.NOOP
    assert "DO-99" in rewritten.notes


def test_apply_third_occurrence_emits_rma_escalate_action() -> None:
    # Phase 3: THIRD_OCCURRENCE now produces a CW0912_RMA_ESCALATE
    # action with the cwctl return-to-triage fallback command. A
    # separate JIRA resolver pass (in the CLI) overlays the HO ticket
    # lookup — when found, command→None and jira_issue is populated.
    actions = [_power_drain_action()]
    cands = [_candidate(stage=CW0912Stage.THIRD_OCCURRENCE)]
    (rewritten,) = apply_cw0912_overrides(actions, cands)
    assert rewritten.kind is ActionKind.CW0912_RMA_ESCALATE
    assert rewritten.command is not None
    assert "return-to-triage" in rewritten.command
    assert "Sending node to RMA" in rewritten.command
    assert rewritten.jira_issue is None  # resolver pass hasn't run yet
    assert "RMA" in rewritten.notes


def test_apply_skips_non_power_drain_actions() -> None:
    # An HO_TICKET action with a matching candidate should be left alone —
    # CW0912 only overrides POWER_DRAIN classifications (not higher-
    # precedence flows like XID_109_RETURN_TO_READY).
    other = PlannedAction(
        bmn="ss900770x4123",
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command="cwctl ...",
        notes="",
    )
    cands = [_candidate(stage=CW0912Stage.SECOND_OCCURRENCE)]
    out = apply_cw0912_overrides([other], cands)
    assert out == [other]


# --------------------------- persist_cw0912_state_after_execute --------------


def test_persist_writes_power_drain_scheduled_on_success() -> None:
    action = _power_drain_action()
    cand = _candidate(stage=CW0912Stage.FIRST_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action, 0)],
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert len(writes) == 1
    assert writes[0].stage == STAGE_POWER_DRAIN_SCHEDULED
    assert writes[0].job_id == cand.current_job_id


def test_persist_writes_tray_reseat_filed_on_success() -> None:
    action = PlannedAction(
        bmn="ss900770x4123",
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.CW0912_TRAY_RESEAT,
        triggering_codes=(CW0912,),
        command="cwctl ticket dct-action device ...",
        notes="",
    )
    cand = _candidate(stage=CW0912Stage.SECOND_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action, 0)],
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert len(writes) == 1
    assert writes[0].stage == STAGE_TRAY_RESEAT_FILED


def test_persist_skips_failed_actions() -> None:
    action = _power_drain_action()
    cand = _candidate(stage=CW0912Stage.FIRST_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action, 1)],  # rc != 0
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert writes == []


def test_persist_skips_power_drain_without_cw0912_in_codes() -> None:
    action = _power_drain_action(codes=("CW0211",))
    cand = _candidate(stage=CW0912Stage.FIRST_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action, 0)],
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert writes == []


# --------------------------- clear_recovered_cw0912_states ------------------


def test_recovery_clear_drops_state_when_cw0912_no_longer_present() -> None:
    # GH200 BMN that previously had CW0912 but the current AWX report
    # doesn't list it anymore — the node is recovering.
    target = _target(codes=(("CW0211", "unrelated"),))
    cleared: list[str] = []
    cleared_returned = clear_recovered_cw0912_states(
        [target],
        state_reader=lambda _b: CW0912State(
            bmn=target.bmn,
            job_id="old",
            observed_at="x",
            stage=STAGE_POWER_DRAIN_SCHEDULED,
        ),
        state_clearer=cleared.append,
    )
    assert cleared == [target.bmn]
    assert cleared_returned == [target.bmn]


def test_recovery_clear_skips_bmns_still_carrying_cw0912() -> None:
    target = _target()  # has CW0912 by default
    cleared: list[str] = []
    clear_recovered_cw0912_states(
        [target],
        state_reader=lambda _b: CW0912State(
            bmn=target.bmn,
            job_id="old",
            observed_at="x",
            stage=STAGE_POWER_DRAIN_SCHEDULED,
        ),
        state_clearer=cleared.append,
    )
    assert cleared == []  # CW0912 still present → state preserved


def test_recovery_clear_skips_non_gh200() -> None:
    target = _target(sku="GPU-H100-02", codes=(("CW0211", "x"),))
    cleared: list[str] = []
    clear_recovered_cw0912_states(
        [target],
        state_reader=lambda _b: CW0912State(
            bmn=target.bmn,
            job_id="old",
            observed_at="x",
            stage=STAGE_POWER_DRAIN_SCHEDULED,
        ),
        state_clearer=cleared.append,
    )
    assert cleared == []


def test_recovery_clear_skips_bmns_without_existing_state() -> None:
    target = _target(codes=(("CW0211", "x"),))
    cleared: list[str] = []
    clear_recovered_cw0912_states(
        [target],
        state_reader=lambda _b: None,
        state_clearer=cleared.append,
    )
    assert cleared == []


# --------------------------- render_cw0912_skipped_summary ------------------


def test_render_summary_empty_when_no_skipped_candidates() -> None:
    assert render_cw0912_skipped_summary([]) == ""


def test_render_summary_includes_stages_and_reasons() -> None:
    in_progress = _candidate(stage=CW0912Stage.IN_PROGRESS)
    deduped = _candidate(
        stage=CW0912Stage.SECOND_OCCURRENCE,
        existing_do_ticket="DO-99",
        bmn="bmn-deduped",
    )
    rendered = render_cw0912_skipped_summary([in_progress, deduped])
    assert "CW0912 candidates not actioned this pass (2)" in rendered
    assert "in_progress" in rendered
    assert "second_occurrence" in rendered
    assert "DO-99" in rendered


def test_render_summary_omits_actionable_third_occurrence() -> None:
    # THIRD_OCCURRENCE is actionable (phase 3 emits CW0912_RMA_ESCALATE),
    # so it appears in the main plan — not in the skipped block.
    rma = _candidate(stage=CW0912Stage.THIRD_OCCURRENCE)
    rendered = render_cw0912_skipped_summary([rma])
    assert rendered == ""


def test_render_summary_omits_actionable_second_occurrence() -> None:
    # An actionable SECOND_OCCURRENCE shows in the main plan, not the
    # "skipped" block.
    actionable = _candidate(stage=CW0912Stage.SECOND_OCCURRENCE)
    assert actionable.actionable is True
    rendered = render_cw0912_skipped_summary([actionable])
    assert rendered == ""


# --------------------------- render_rma_template ----------------------------


def test_render_rma_template_substitutes_identifier_sn_and_template_marker() -> None:
    body = render_rma_template(cw_node="g75cb28", bmn="ss900770x4123", serial="S900770X4123")
    assert body.startswith("RMA to Vendor")
    # cw_node wins for Device Identifier when present.
    assert "Device Identifier: g75cb28" in body
    assert "Device SN: S900770X4123" in body
    # Component fields are intentionally blank for the onsite tech.
    assert "Component: GPU Tray" in body
    assert "Component SN: \n" in body
    assert "Component Slot: \n" in body
    assert "Point of Failure:" in body
    # Required logs carries the auto-fill marker so downstream sees it.
    assert "Required logs:\n(this will be auto filled)" in body


def test_render_rma_template_falls_back_to_bmn_when_cw_node_empty() -> None:
    body = render_rma_template(cw_node="", bmn="ss900770x4999", serial="SN-X")
    assert "Device Identifier: ss900770x4999" in body


# --------------------------- rma_return_to_triage_command -------------------


def test_rma_return_to_triage_command_matches_documented_form() -> None:
    cmd = rma_return_to_triage_command("ss900770x4123")
    assert "cwctl flcc node -w return-to-triage ss900770x4123" in cmd
    # `-o` triggers HO ticket creation and the `-m` text mentions RMA so
    # the resulting ticket has the right context for the operator.
    assert " -o -m " in cmd
    assert "Sending node to RMA" in cmd


# --------------------------- resolve_cw0912_rma_tickets ---------------------


def _rma_action(bmn: str = "ss900770x4123") -> PlannedAction:
    return PlannedAction(
        bmn=bmn,
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.CW0912_RMA_ESCALATE,
        triggering_codes=(CW0912,),
        command=rma_return_to_triage_command(bmn),
        notes="",
    )


def test_resolve_rma_swaps_to_jira_issue_when_search_finds_open_ho() -> None:
    actions = [_rma_action()]
    target = _target()
    out = resolve_cw0912_rma_tickets(
        actions,
        search_fn=lambda _ids: "HO-42",
        targets_by_bmn={target.bmn: target},
    )
    (action,) = out
    assert action.kind is ActionKind.CW0912_RMA_ESCALATE
    assert action.jira_issue == "HO-42"
    assert action.command is None  # cwctl fallback replaced
    assert "HO-42" in action.notes


def test_resolve_rma_leaves_action_intact_when_no_ho_found() -> None:
    actions = [_rma_action()]
    target = _target()
    out = resolve_cw0912_rma_tickets(
        actions,
        search_fn=lambda _ids: None,
        targets_by_bmn={target.bmn: target},
    )
    (action,) = out
    assert action.jira_issue is None
    assert action.command is not None
    assert "return-to-triage" in action.command


def test_resolve_rma_skips_non_rma_actions() -> None:
    other = PlannedAction(
        bmn="ss900770x4123",
        cw_node="g1",
        sku=GH200_SKU,
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command="cwctl ...",
        notes="",
    )
    out = resolve_cw0912_rma_tickets(
        [other],
        search_fn=lambda _ids: "HO-999",
        targets_by_bmn={},
    )
    assert out == [other]  # HO_TICKET resolution lives in a separate path


def test_resolve_rma_uses_full_search_identifiers_from_target() -> None:
    target = BMNTarget(
        bmn="ss900770x4123",
        cw_node="g75cb28",
        sku=GH200_SKU,
        awx_reports=(),
        serial="S900770X4123",
    )
    received: list[tuple[str, ...]] = []

    def _search(ids: tuple[str, ...]) -> str | None:
        received.append(ids)
        return None

    resolve_cw0912_rma_tickets(
        [_rma_action()],
        search_fn=_search,
        targets_by_bmn={target.bmn: target},
    )
    assert received == [target.search_identifiers]
    assert "ss900770x4123" in received[0]
    assert "g75cb28" in received[0]
    assert "S900770X4123" in received[0]


# --------------------------- persist (RMA stage) ---------------------------


def test_persist_writes_rma_escalated_only_when_jira_comment_landed() -> None:
    action_with_jira = PlannedAction(
        bmn="ss900770x4123",
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.CW0912_RMA_ESCALATE,
        triggering_codes=(CW0912,),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    cand = _candidate(stage=CW0912Stage.THIRD_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action_with_jira, 0)],
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert len(writes) == 1
    assert writes[0].stage == STAGE_RMA_ESCALATED
    assert writes[0].job_id == cand.current_job_id


def test_persist_skips_state_write_when_rma_action_used_cwctl_fallback() -> None:
    # cwctl just created the HO ticket; the RMA comment hasn't landed
    # yet (operator must re-run --action). We must NOT advance to
    # RMA_ESCALATED — otherwise the next pass would treat the BMN as
    # already-escalated and not add the comment.
    action_cwctl = PlannedAction(
        bmn="ss900770x4123",
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.CW0912_RMA_ESCALATE,
        triggering_codes=(CW0912,),
        command=rma_return_to_triage_command("ss900770x4123"),
        notes="",
        jira_issue=None,
    )
    cand = _candidate(stage=CW0912Stage.THIRD_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action_cwctl, 0)],
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert writes == []


def test_persist_skips_rma_on_failed_execution() -> None:
    action_with_jira = PlannedAction(
        bmn="ss900770x4123",
        cw_node="g75cb28",
        sku=GH200_SKU,
        kind=ActionKind.CW0912_RMA_ESCALATE,
        triggering_codes=(CW0912,),
        command=None,
        notes="",
        jira_issue="HO-42",
    )
    cand = _candidate(stage=CW0912Stage.THIRD_OCCURRENCE)
    writes: list[CW0912State] = []
    persist_cw0912_state_after_execute(
        [(action_with_jira, 1)],  # rc != 0
        [cand],
        state_writer=writes.append,
        now=lambda: "2026-06-11T17:00:00Z",
    )
    assert writes == []
