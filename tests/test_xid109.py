"""Tests for the XID-109 return-to-ready pipeline."""

from __future__ import annotations

from frops.action import BMNTarget
from frops.xid109 import (
    NLCC_OWNS_REASON,
    XID109Candidate,
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


# --------------------------- collect_xid109_candidates ----------------------


def test_collect_filters_to_triage_then_jira_then_description() -> None:
    targets = [
        _target(bmn="triage-xid"),  # full pipeline pass → actionable
        _target(bmn="triage-noxid"),  # JIRA matches but no XID 109 in desc
        _target(bmn="triage-nojira"),  # no JIRA match
        _target(bmn="not-triage"),  # filtered out at stage 1
    ]
    cwnc_states = {
        "triage-xid": "triage",
        "triage-noxid": "triage",
        "triage-nojira": "triage",
        "not-triage": "production",
    }
    jira_search_calls: list[tuple[str, ...]] = []

    def _search(identifiers: tuple[str, ...]) -> str | None:
        jira_search_calls.append(identifiers)
        if "triage-xid" in identifiers:
            return "HO-1"
        if "triage-noxid" in identifiers:
            return "HO-2"
        return None  # triage-nojira

    descriptions = {
        "HO-1": "Server failed XID 109 in production",
        "HO-2": "Server is reporting some other condition; XID 79",
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

    # Only triage-xid survives the full pipeline.
    assert [c.bmn for c in candidates] == ["triage-xid"]
    assert candidates[0].actionable is True
    # not-triage was filtered before JIRA was queried for it.
    assert all("not-triage" not in ids for ids in jira_search_calls)
    # phase fetch only happens for BMNs that survive the description filter.
    assert fetch_phase_calls == ["triage-xid"]


def test_collect_emits_waiting_when_phase_reason_is_not_nlcc() -> None:
    targets = [_target(bmn="bmn-waiting")]
    candidates = collect_xid109_candidates(
        targets,
        {"bmn-waiting": "triage"},
        jira_search=lambda _ids: "HO-99",
        fetch_description=lambda _key: "Saw XID 109 in console",
        fetch_phase=lambda _bmn: "flcc",
    )
    (cand,) = candidates
    assert cand.bmn == "bmn-waiting"
    assert cand.actionable is False
    assert cand.phase_reason == "flcc"


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
