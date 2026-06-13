"""Tests for the XID-109 return-to-ready pipeline."""

from __future__ import annotations

from frops.action import BMNTarget
from frops.xid109 import (
    HO_PROJECT,
    NLCC_OWNS_REASON,
    XID109Candidate,
    build_xid109_ho_search_jql,
    classify_xid109_target,
    collect_xid109_candidates,
    description_mentions_xid_109,
    parse_cwnc_states,
    render_actionable_summary,
    render_waiting_summary,
    return_to_ready_command,
    split_by_actionable,
)


def _target(
    *,
    bmn: str = "ss900770x4120327",
    cw_node: str = "g826cb0",
    sku: str = "GPU-H100-02",
    serial: str = "S900770X4120327",
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku=sku,
        awx_reports=(),
        serial=serial,
    )


# --------------------------- description matcher ----------------------------


def test_description_mentions_xid_109_matches_spaced_form() -> None:
    assert description_mentions_xid_109("This is XID 109 noise.") is True


def test_description_mentions_xid_109_matches_no_separator() -> None:
    # As seen in tokens like "GPUContextSwitchTimeoutXid109".
    assert description_mentions_xid_109("...GPUContextSwitchTimeoutXid109...") is True


def test_description_mentions_xid_109_matches_punctuation_variants() -> None:
    assert description_mentions_xid_109("xid-109") is True
    assert description_mentions_xid_109("XID_109") is True


def test_description_mentions_xid_109_is_case_insensitive() -> None:
    assert description_mentions_xid_109("xid 109") is True
    assert description_mentions_xid_109("Xid 109") is True
    assert description_mentions_xid_109("XID  109") is True


def test_description_mentions_xid_109_rejects_unrelated() -> None:
    # XID 79 — different code.
    assert description_mentions_xid_109("Saw XID 79 today") is False
    # No mention at all.
    assert description_mentions_xid_109("RMCP+ session timeout, retry needed") is False
    # Empty / whitespace.
    assert description_mentions_xid_109("") is False
    assert description_mentions_xid_109("   \n  ") is False


def test_description_mentions_xid_109_rejects_substring_in_longer_number() -> None:
    # `\b` anchors prevent matching "1090" or "21091".
    assert description_mentions_xid_109("XID 1090") is False
    assert description_mentions_xid_109("XID2109") is False


# --------------------------- parse_cwnc_states ------------------------------


_BMNS_WIDE_MULTI = (
    "NAME       DEVICESLOT        CW-NODE   EXISTS   ONLINE   CW-SKU        "
    "BMC-IP         OWNER        CLUSTER                    CWNC-STATE   "
    "WORKFLOW   RETURN-WORKFLOW   RETURN-STATE   RETURN-STEP   PREV-WORKFLOW-STEP   "
    "WORKFLOW-STEP   NEXT-WORKFLOW-STEP   PREV-STATE   STATE        NEXT-STATE   "
    "TS     ORG-ID   NODE-PROFILE\n"
    "bmn-a      s4-r267-node-02   g111aaa   true     true     GPU-H100-02   "
    "10.0.0.1       unassigned   tenant-x                   triage       "
    "orphan     empty             empty          empty         production           "
    "empty           empty                production   triage       empty        "
    "1d     org-x    profile-y\n"
    "bmn-b      s4-r267-node-03   g222bbb   true     true     GPU-H100-02   "
    "10.0.0.2       unassigned   tenant-x                   production   "
    "orphan     empty             empty          empty         production           "
    "empty           empty                production   production   empty        "
    "1d     org-x    profile-y\n"
)


def test_parse_cwnc_states_extracts_name_and_state_per_row() -> None:
    states = parse_cwnc_states(_BMNS_WIDE_MULTI)
    assert states == {"bmn-a": "triage", "bmn-b": "production"}


def test_parse_cwnc_states_returns_empty_on_missing_header() -> None:
    # No CWNC-STATE column at all → can't extract; return empty so the
    # XID-109 pipeline degrades quietly.
    assert parse_cwnc_states("NAME   STATE\nbmn-x  fail\n") == {}


def test_parse_cwnc_states_returns_empty_on_empty_input() -> None:
    assert parse_cwnc_states("") == {}
    assert parse_cwnc_states("only-header\n") == {}


# --------------------------- build_xid109_ho_search_jql ---------------------


def test_xid109_ho_jql_quotes_HO_and_uses_status_category_not_done() -> None:
    jql = build_xid109_ho_search_jql(("ss900770x4123", "g75cb28"))
    assert jql is not None
    # `HO` would be safe unquoted but we quote for parity with the DO
    # builders + any future project key that collides with reserved JQL.
    assert f'project = "{HO_PROJECT}"' in jql
    # `statusCategory != "Done"` is the wider gate replacing the previous
    # `status = "Awaiting Support"` filter — that narrow filter was
    # silently dropping XID-109 tickets in other in-progress statuses.
    assert 'statusCategory != "Done"' in jql
    # Identifiers feed BOTH summary AND description clauses so an HO
    # ticket that mentions the BMN only in its body still surfaces.
    for ident in ("ss900770x4123", "g75cb28"):
        assert f'summary ~ "{ident}"' in jql
        assert f'description ~ "{ident}"' in jql


def test_xid109_ho_jql_returns_none_when_no_identifiers() -> None:
    assert build_xid109_ho_search_jql(()) is None
    assert build_xid109_ho_search_jql(("", "")) is None


def test_xid109_ho_jql_escapes_double_quotes() -> None:
    jql = build_xid109_ho_search_jql(('weird"id',))
    assert jql is not None
    assert r"weird\"id" in jql


# --------------------------- classify_xid109_target -------------------------


def test_classify_actionable_when_phase_reason_is_nlcc() -> None:
    cand = classify_xid109_target(
        target=_target(),
        cwnc_state="triage",
        phase_reason=NLCC_OWNS_REASON,
        jira_issue="HO-12345",
    )
    assert cand.actionable is True
    assert cand.jira_issue == "HO-12345"
    assert cand.cwnc_state == "triage"


def test_classify_waiting_when_phase_reason_is_anything_else() -> None:
    for reason in ("flcc", "production", "", "weird-value"):
        cand = classify_xid109_target(
            target=_target(),
            cwnc_state="triage",
            phase_reason=reason,
            jira_issue="HO-1",
        )
        assert cand.actionable is False, f"reason {reason!r} should be waiting"


def test_classify_waiting_when_cwnc_state_not_yet_triage() -> None:
    # Phase reason says nlcc but CWNC-STATE hasn't propagated to triage
    # yet — node still in transit, not actionable. Without the cwnc_state
    # gate in classify this would be incorrectly marked actionable.
    for state in ("draining", "post-prod", "", "production"):
        cand = classify_xid109_target(
            target=_target(),
            cwnc_state=state,
            phase_reason=NLCC_OWNS_REASON,
            jira_issue="HO-1",
        )
        assert cand.actionable is False, f"cwnc_state {state!r} should be waiting"


# --------------------------- collect_xid109_candidates ----------------------


def test_collect_filters_via_jira_then_description_not_cwnc_state() -> None:
    # The CWNC-STATE filter is NOT a hard gate any more — XID-109 nodes
    # that have exited production but haven't propagated to CWNC-STATE
    # =triage yet still need to surface (under the waiting list) so the
    # operator sees the full fall-out. The HO description match remains
    # the authoritative inclusion signal.
    targets = [
        _target(bmn="triage-xid"),  # CWNC-STATE=triage + XID 109 desc + nlcc → actionable
        _target(bmn="in-transit-xid"),  # CWNC-STATE not yet triage + XID 109 desc → waiting
        _target(bmn="triage-noxid"),  # CWNC-STATE=triage, HO matches but no XID 109 → dropped
        _target(bmn="triage-nojira"),  # CWNC-STATE=triage, no HO match → dropped
    ]
    cwnc_states = {
        "triage-xid": "triage",
        "in-transit-xid": "draining",  # exited production but not yet at triage
        "triage-noxid": "triage",
        "triage-nojira": "triage",
    }
    jira_search_calls: list[tuple[str, ...]] = []

    def _search(identifiers: tuple[str, ...]) -> str | None:
        jira_search_calls.append(identifiers)
        if "triage-xid" in identifiers:
            return "HO-1"
        if "in-transit-xid" in identifiers:
            return "HO-3"
        if "triage-noxid" in identifiers:
            return "HO-2"
        return None  # triage-nojira

    descriptions = {
        "HO-1": "Server failed XID 109 in production",
        "HO-2": "Server is reporting some other condition; XID 79",
        "HO-3": "Node hit XID 109 and exited production",
    }
    fetch_phase_calls: list[str] = []

    def _fetch_phase(bmn: str) -> str:
        fetch_phase_calls.append(bmn)
        return NLCC_OWNS_REASON if bmn == "triage-xid" else "flcc"

    candidates = collect_xid109_candidates(
        targets,
        cwnc_states,
        jira_search=_search,
        fetch_description=lambda key: descriptions.get(key, ""),
        fetch_phase=_fetch_phase,
    )

    # Both XID-109 nodes survive — the one at triage and the one still in transit.
    assert {c.bmn for c in candidates} == {"triage-xid", "in-transit-xid"}
    by_bmn = {c.bmn: c for c in candidates}
    # Only the one at triage+nlcc is actionable.
    assert by_bmn["triage-xid"].actionable is True
    # The in-transit one shows up but isn't actionable — it lands in the
    # waiting list so the operator still sees it.
    assert by_bmn["in-transit-xid"].actionable is False
    assert by_bmn["in-transit-xid"].cwnc_state == "draining"
    # JIRA is queried for every target now (cwnc_state isn't a gate).
    assert len(jira_search_calls) == len(targets)
    # phase fetch runs only for BMNs that survive the description filter.
    assert sorted(fetch_phase_calls) == ["in-transit-xid", "triage-xid"]


def test_collect_marks_triage_with_non_nlcc_phase_as_waiting() -> None:
    # CWNC-STATE=triage but PhaseState reason=flcc → not yet eligible for
    # return-to-ready; should appear in the waiting list, not actionable.
    targets = [_target(bmn="bmn-waiting")]
    candidates = collect_xid109_candidates(
        targets,
        {"bmn-waiting": "triage"},
        jira_search=lambda _ids: "HO-99",
        fetch_description=lambda _key: "Saw XID 109 in console",
        fetch_phase=lambda _bmn: "flcc",
    )
    (cand,) = candidates
    assert cand.actionable is False
    assert cand.phase_reason == "flcc"
    assert cand.cwnc_state == "triage"


# --------------------------- split_by_actionable ----------------------------


def test_split_by_actionable_partitions_correctly() -> None:
    a = XID109Candidate(
        bmn="a",
        cw_node="g1",
        sku="x",
        cwnc_state="triage",
        phase_reason="nlcc",
        jira_issue="HO-1",
        actionable=True,
    )
    b = XID109Candidate(
        bmn="b",
        cw_node="g2",
        sku="x",
        cwnc_state="triage",
        phase_reason="flcc",
        jira_issue="HO-2",
        actionable=False,
    )
    actionable, waiting = split_by_actionable([a, b])
    assert actionable == [a]
    assert waiting == [b]


# --------------------------- return_to_ready_command ------------------------


def test_return_to_ready_command_contains_bmn_and_message() -> None:
    cmd = return_to_ready_command("ss900770x4120327")
    assert "cwctl flcc node -w return-to-ready ss900770x4120327" in cmd
    assert "sending node back to ready" in cmd
    assert "XID 109" in cmd


# --------------------------- render_waiting_summary -------------------------


def test_render_waiting_summary_empty_returns_empty_string() -> None:
    assert render_waiting_summary([]) == ""


def test_render_waiting_summary_renders_table_with_details() -> None:
    waiting = [
        XID109Candidate(
            bmn="ss900770x4120327",
            cw_node="g826cb0",
            sku="GPU-H100-02",
            cwnc_state="triage",
            phase_reason="flcc",
            jira_issue="HO-12345",
            actionable=False,
        ),
        XID109Candidate(
            bmn="ss900770x4200980",
            cw_node="g81b512",
            sku="GPU-H100-02",
            cwnc_state="triage",
            phase_reason="",
            jira_issue="HO-67890",
            actionable=False,
        ),
    ]
    rendered = render_waiting_summary(waiting)
    assert "=== XID 109 BMNs waiting for return-to-fleetops (2) ===" in rendered
    for token in ("BMN", "CW-NODE", "CWNC-STATE", "PHASE-REASON", "HO TICKET"):
        assert token in rendered
    for value in ("ss900770x4120327", "g826cb0", "HO-12345", "flcc"):
        assert value in rendered
    # Empty phase reason falls back to "(unknown)".
    assert "(unknown)" in rendered


# --------------------------- render_actionable_summary ----------------------


def test_render_actionable_summary_empty_returns_empty_string() -> None:
    assert render_actionable_summary([]) == ""


def test_render_actionable_summary_renders_table_with_details() -> None:
    actionable = [
        XID109Candidate(
            bmn="ss900770x4321000",
            cw_node="g999aaa",
            sku="GPU-H100-01",
            cwnc_state="triage",
            phase_reason=NLCC_OWNS_REASON,
            jira_issue="HO-22222",
            actionable=True,
        ),
        XID109Candidate(
            bmn="ss900770x4321001",
            cw_node="g999bbb",
            sku="GPU-H100-01",
            cwnc_state="triage",
            phase_reason=NLCC_OWNS_REASON,
            jira_issue="HO-22223",
            actionable=True,
        ),
    ]
    rendered = render_actionable_summary(actionable)
    # Count + section header.
    assert "=== XID 109 BMNs ready for return-to-ready (2) ===" in rendered
    # Intro mentions both flcc-triage and nlcc-triage so the operator knows
    # exactly why these BMNs are eligible.
    assert "flcc-triage" in rendered and "nlcc-triage" in rendered
    # Same column layout as the waiting table.
    for token in ("BMN", "CW-NODE", "CWNC-STATE", "PHASE-REASON", "HO TICKET"):
        assert token in rendered
    for value in ("ss900770x4321000", "g999aaa", "HO-22222", NLCC_OWNS_REASON):
        assert value in rendered


def test_render_actionable_and_waiting_summaries_are_visually_distinct() -> None:
    # The two tables share a column layout but their titles + intros must
    # differ so the operator can tell at a glance which group they're in.
    candidate = XID109Candidate(
        bmn="ss900770x4000001",
        cw_node="g1",
        sku="GPU-H100-01",
        cwnc_state="triage",
        phase_reason=NLCC_OWNS_REASON,
        jira_issue="HO-1",
        actionable=True,
    )
    actionable = render_actionable_summary([candidate])
    waiting = render_waiting_summary([candidate])
    assert "ready for return-to-ready" in actionable
    assert "ready for return-to-ready" not in waiting
    assert "waiting for return-to-fleetops" in waiting
    assert "waiting for return-to-fleetops" not in actionable
