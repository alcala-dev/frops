"""Tests for the IBP reseat DCT ticket workflow."""

from __future__ import annotations

from frops.action import ActionKind, BMNTarget, PlannedAction
from frops.ibp_reseat import (
    DO_PROJECT,
    GENERIC_IBP_LABEL,
    IBP_TICKET_KEYWORDS,
    IBPReseatCandidate,
    apply_ibp_reseat_overrides,
    build_do_search_jql,
    collect_ibp_reseat_candidates,
    description_mentions_ibp,
    find_ibp_label_in_description,
    ibp_reseat_command,
    render_ibp_skipped_summary,
)


def _target(
    *,
    bmn: str = "ss900770x4200980",
    cw_node: str = "g1cb982",
    serial: str = "S900770X4200980",
    region: str = "RNO2A",
    sku: str = "GPU-H100-02",
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku=sku,
        awx_reports=(),
        serial=serial,
        region=region,
    )


def _ho_ticket_action(bmn: str = "ss900770x4200980", cw_node: str = "g1cb982") -> PlannedAction:
    return PlannedAction(
        bmn=bmn,
        cw_node=cw_node,
        sku="GPU-H100-02",
        kind=ActionKind.HO_TICKET,
        triggering_codes=("CW0201",),
        command="cwctl flcc node -w return-to-triage ... -o -m '...'",
        notes="HO-ticket cwctl fallback",
    )


# --------------------------- description_mentions_ibp -----------------------


def test_description_matches_specific_ibp_number() -> None:
    desc = "g1cb982 ibp2 has flapped multiple times over the past week"
    assert description_mentions_ibp(desc) is True


def test_description_matches_generic_ibp_word() -> None:
    desc = "Backend ibp link is bouncing intermittently"
    assert description_mentions_ibp(desc) is True


def test_description_matches_alert_reason() -> None:
    # Some HO tickets only carry the Alertmanager rule name in the
    # description (no literal `ibp` in the surrounding prose).
    desc = 'reason="Alert triggered IBMultipleFlaps"'
    assert description_mentions_ibp(desc) is True


def test_description_is_case_insensitive() -> None:
    assert description_mentions_ibp("ibmultipleflaps") is True
    assert description_mentions_ibp("IBP3 down") is True


def test_description_rejects_unrelated_text() -> None:
    assert description_mentions_ibp("") is False
    assert description_mentions_ibp("Xid 109 detected") is False
    assert description_mentions_ibp("just some random text") is False


def test_description_rejects_substring_in_unrelated_word() -> None:
    # Must be a word boundary — "kibplus" shouldn't match the `ibp` substring.
    assert description_mentions_ibp("the kibplus tool reported nothing") is False


# --------------------------- find_ibp_label_in_description -------------------


def test_find_ibp_label_extracts_numbered_interface() -> None:
    assert find_ibp_label_in_description("g1cb982 ibp2 has flapped") == "ibp2"
    assert find_ibp_label_in_description("ibp0 / ibp7 see") == "ibp0"  # first wins


def test_find_ibp_label_falls_back_to_generic_when_no_number() -> None:
    assert find_ibp_label_in_description("Alert: IBMultipleFlaps") == GENERIC_IBP_LABEL
    assert find_ibp_label_in_description("Backend ibp link is bouncing") == GENERIC_IBP_LABEL


def test_find_ibp_label_handles_empty_input() -> None:
    assert find_ibp_label_in_description("") == GENERIC_IBP_LABEL


# --------------------------- ibp_reseat_command -----------------------------


def test_ibp_reseat_command_renders_specific_interface() -> None:
    cmd = ibp_reseat_command(cw_node="g1cb982", serial="S1", region="RNO2A", ibp_label="ibp2")
    assert "cwctl ticket dct-action network S1" in cmd
    # The specific ibp label appears in both the "X down" prefix and the
    # "reseat and clean X" instruction.
    assert "ibp2 down" in cmd
    assert "reseat and clean ibp2" in cmd
    assert "(g1cb982 | SN: S1)" in cmd
    assert "-r RNO2A" in cmd


def test_ibp_reseat_command_falls_back_to_generic_label() -> None:
    cmd = ibp_reseat_command(cw_node="g1", serial="S1", region="RNO2A", ibp_label=GENERIC_IBP_LABEL)
    assert "ibp down" in cmd
    assert "reseat and clean ibp on node" in cmd


# --------------------------- build_do_search_jql ----------------------------


def test_build_do_search_jql_filters_to_DO_project_open_status_and_keywords() -> None:
    jql = build_do_search_jql(("ss900770x4200980", "g1cb982", "S900770X4200980"))
    assert jql is not None
    # Quoted because `DO` is a reserved JQL word.
    assert f'project = "{DO_PROJECT}"' in jql
    # Status filter excludes closed tickets so a previously-closed reseat
    # doesn't suppress a new flap.
    assert "statusCategory" in jql and "Done" in jql
    # All identifiers feed the OR'd id clause.
    for ident in ("ss900770x4200980", "g1cb982", "S900770X4200980"):
        assert ident in jql
    # Drive/reseat/fiber keywords appear.
    for kw in IBP_TICKET_KEYWORDS:
        assert kw in jql


def test_build_do_search_jql_returns_none_when_no_identifiers() -> None:
    assert build_do_search_jql(()) is None
    assert build_do_search_jql(("", "")) is None


def test_build_do_search_jql_drops_empty_identifiers() -> None:
    jql = build_do_search_jql(("", "real-bmn", ""))
    assert jql is not None
    assert "real-bmn" in jql


def test_build_do_search_jql_escapes_double_quotes() -> None:
    jql = build_do_search_jql(('weird"id',))
    assert jql is not None
    assert r"weird\"id" in jql


# --------------------------- collect_ibp_reseat_candidates -------------------


def test_collect_filters_triage_then_ho_then_description() -> None:
    # PhaseState reason is captured for diagnostic surfacing but does NOT
    # gate eligibility — IBMultipleFlaps is an FLCC alert so most ibp-flap
    # BMNs sit at `phase_reason="flcc"` and would never reach nlcc handoff.
    targets = [
        _target(bmn="nlcc-node"),  # passes; phase reason happens to be nlcc
        _target(bmn="flcc-node"),  # passes; phase reason still flcc (the bug fix)
        _target(bmn="no-triage"),  # CWNC-STATE filter drops
        _target(bmn="no-ho"),  # HO search returns None
        _target(bmn="no-ibp"),  # HO matches but description has no ibp
    ]
    cwnc_states = {
        "nlcc-node": "triage",
        "flcc-node": "triage",
        "no-triage": "production",
        "no-ho": "triage",
        "no-ibp": "triage",
    }
    descriptions_by_bmn = {
        "nlcc-node": "g1 ibp2 has flapped",
        "flcc-node": "ibp0 link down",
        "no-ho": "(never fetched — no ticket)",
        "no-ibp": "Xid 109 detected on the node",
    }
    ho_calls: list[tuple[str, ...]] = []

    def _ho_search(ids: tuple[str, ...]) -> str | None:
        ho_calls.append(ids)
        if "no-ho" in ids:
            return None
        if "nlcc-node" in ids:
            return "HO-1"
        if "no-ibp" in ids:
            return "HO-2"
        if "flcc-node" in ids:
            return "HO-3"
        return None

    def _fetch_desc(key: str) -> str:
        return {
            "HO-1": descriptions_by_bmn["nlcc-node"],
            "HO-2": descriptions_by_bmn["no-ibp"],
            "HO-3": descriptions_by_bmn["flcc-node"],
        }.get(key, "")

    def _fetch_phase(bmn: str) -> str:
        return "nlcc" if bmn == "nlcc-node" else "flcc"

    candidates = collect_ibp_reseat_candidates(
        targets,
        cwnc_states,
        ho_search=_ho_search,
        fetch_description=_fetch_desc,
        fetch_phase=_fetch_phase,
        do_search=lambda _ids: (None, None),
    )

    # Both nlcc-node and flcc-node make it through; phase_reason no longer gates.
    assert {c.bmn for c in candidates} == {"nlcc-node", "flcc-node"}
    by_bmn = {c.bmn: c for c in candidates}
    assert by_bmn["nlcc-node"].actionable is True
    assert by_bmn["nlcc-node"].ibp_label == "ibp2"
    assert by_bmn["nlcc-node"].ho_ticket == "HO-1"
    assert by_bmn["nlcc-node"].phase_reason == "nlcc"
    assert by_bmn["flcc-node"].actionable is True
    assert by_bmn["flcc-node"].ibp_label == "ibp0"
    assert by_bmn["flcc-node"].phase_reason == "flcc"
    # no-triage was filtered upfront, so HO search never ran for it.
    assert all("no-triage" not in ids for ids in ho_calls)


def test_collect_records_existing_do_ticket_skipping_creation() -> None:
    target = _target()
    candidates = collect_ibp_reseat_candidates(
        [target],
        {target.bmn: "triage"},
        ho_search=lambda _ids: "HO-9",
        fetch_description=lambda _k: "ibp1 down",
        fetch_phase=lambda _b: "nlcc",
        do_search=lambda _ids: ("DO-42", None),
    )
    (cand,) = candidates
    assert cand.existing_do_ticket == "DO-42"
    assert cand.actionable is False
    assert cand.ibp_label == "ibp1"


def test_collect_records_do_search_error_skipping_creation() -> None:
    target = _target()
    candidates = collect_ibp_reseat_candidates(
        [target],
        {target.bmn: "triage"},
        ho_search=lambda _ids: "HO-9",
        fetch_description=lambda _k: "ibp2 down",
        fetch_phase=lambda _b: "nlcc",
        do_search=lambda _ids: (None, "HTTP 500"),
    )
    (cand,) = candidates
    assert cand.error == "HTTP 500"
    assert cand.actionable is False


# --------------------------- apply_ibp_reseat_overrides ---------------------


def test_apply_overrides_promotes_ho_action_to_ibp_reseat() -> None:
    action = _ho_ticket_action(bmn="bmn-x", cw_node="g-x")
    target = _target(bmn="bmn-x", cw_node="g-x")
    cand = IBPReseatCandidate(
        bmn="bmn-x",
        cw_node="g-x",
        sku=target.sku,
        serial=target.serial,
        region=target.region,
        phase_reason="nlcc",
        ho_ticket="HO-1",
        ibp_label="ibp2",
        existing_do_ticket=None,
        error=None,
    )
    rewritten = apply_ibp_reseat_overrides([action], [cand])
    (got,) = rewritten
    assert got.kind is ActionKind.IBP_RESEAT
    assert got.command is not None
    assert "ibp2 down" in got.command
    assert f"-r {target.region}" in got.command
    assert f"network {target.serial}" in got.command
    assert "HO-1" in got.notes


def test_apply_overrides_downgrades_to_noop_when_existing_do_ticket() -> None:
    action = _ho_ticket_action(bmn="bmn-y")
    cand = IBPReseatCandidate(
        bmn="bmn-y",
        cw_node="g-y",
        sku="GPU-H100-02",
        serial="S",
        region="RNO2A",
        phase_reason="nlcc",
        ho_ticket="HO-2",
        ibp_label="ibp",
        existing_do_ticket="DO-99",
        error=None,
    )
    rewritten = apply_ibp_reseat_overrides([action], [cand])
    (got,) = rewritten
    assert got.kind is ActionKind.NOOP
    assert got.command is None
    assert "DO-99" in got.notes
    assert "HO-2" in got.notes


def test_apply_overrides_leaves_higher_precedence_actions_alone() -> None:
    """Power-drain / drive-inspect / xid-109 win against IBP_RESEAT;
    the resolver mustn't clobber them even if the BMN also has an HO+ibp
    match."""
    power_drain = PlannedAction(
        bmn="bmn-z",
        cw_node="g-z",
        sku="GPU-H100-02",
        kind=ActionKind.POWER_DRAIN,
        triggering_codes=("CW0211",),
        command="cwctl flcc node ... power-drain",
        notes="...",
    )
    cand = IBPReseatCandidate(
        bmn="bmn-z",
        cw_node="g-z",
        sku="GPU-H100-02",
        serial="S",
        region="RNO2A",
        phase_reason="nlcc",
        ho_ticket="HO-1",
        ibp_label="ibp1",
        existing_do_ticket=None,
        error=None,
    )
    rewritten = apply_ibp_reseat_overrides([power_drain], [cand])
    assert rewritten == [power_drain]


# --------------------------- render_ibp_skipped_summary ---------------------


def test_render_ibp_skipped_summary_empty_when_all_actionable() -> None:
    cand = IBPReseatCandidate(
        bmn="x",
        cw_node="g",
        sku="GPU-H100-02",
        serial="S",
        region="RNO2A",
        phase_reason="nlcc",
        ho_ticket="HO-1",
        ibp_label="ibp1",
        existing_do_ticket=None,
        error=None,
    )
    assert render_ibp_skipped_summary([cand]) == ""


def test_render_ibp_skipped_summary_lists_existing_and_error_cases() -> None:
    candidates = [
        IBPReseatCandidate(
            bmn="bmn-existing",
            cw_node="g-1",
            sku="GPU-H100-02",
            serial="S1",
            region="RNO2A",
            phase_reason="nlcc",
            ho_ticket="HO-A",
            ibp_label="ibp1",
            existing_do_ticket="DO-77",
            error=None,
        ),
        IBPReseatCandidate(
            bmn="bmn-errored",
            cw_node="g-2",
            sku="GPU-H100-02",
            serial="S2",
            region="RNO2A",
            phase_reason="nlcc",
            ho_ticket="HO-B",
            ibp_label="ibp",
            existing_do_ticket=None,
            error="HTTP 500",
        ),
    ]
    rendered = render_ibp_skipped_summary(candidates)
    assert "=== IBP reseat candidates skipped (2) ===" in rendered
    assert "bmn-existing" in rendered and "DO-77" in rendered and "HO-A" in rendered
    assert "bmn-errored" in rendered and "HTTP 500" in rendered and "HO-B" in rendered
