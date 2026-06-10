"""Tests for the DO ticket dedup resolver."""

from __future__ import annotations

from frops.action import ActionKind, BMNTarget, PlannedAction
from frops.drive_inspect import (
    DO_PROJECT,
    DRIVE_KEYWORDS,
    DriveInspectResolution,
    build_search_jql,
    resolve_drive_inspect,
)


def _target(
    *,
    bmn: str = "ss900770x4200980",
    cw_node: str = "g81b512",
    serial: str = "S900770X4200980",
    region: str = "RNO2",
) -> BMNTarget:
    return BMNTarget(
        bmn=bmn,
        cw_node=cw_node,
        sku="GPU-GH200-01",
        awx_reports=(),
        serial=serial,
        region=region,
    )


def _drive_inspect_action(
    bmn: str = "ss900770x4200980",
    cw_node: str = "g81b512",
) -> PlannedAction:
    return PlannedAction(
        bmn=bmn,
        cw_node=cw_node,
        sku="GPU-GH200-01",
        kind=ActionKind.DRIVE_INSPECT,
        triggering_codes=("CW0810",),
        command="cwctl ticket dct-action device SN -m '...' -r RNO2",
        notes="Detected CW0810 → would file DO ticket.",
    )


# --------------------------- build_search_jql -------------------------------


def test_build_search_jql_uses_DO_project_and_drive_keywords() -> None:
    jql = build_search_jql(("ss900770x4200980", "g81b512", "S900770X4200980"))
    assert jql is not None
    assert f"project = {DO_PROJECT}" in jql
    # All three identifiers contribute to the OR'd id clause.
    for ident in ("ss900770x4200980", "g81b512", "S900770X4200980"):
        assert ident in jql
    # All three drive keywords appear in the keyword clause.
    for kw in DRIVE_KEYWORDS:
        assert kw in jql
    # AND between the two clauses; no status filter (so closed tickets count).
    assert " AND " in jql
    assert "status" not in jql


def test_build_search_jql_returns_none_when_no_usable_identifiers() -> None:
    # All empty → can't query usefully; returning None tells the caller to
    # skip without hitting JIRA.
    assert build_search_jql(()) is None
    assert build_search_jql(("", "")) is None


def test_build_search_jql_drops_empty_identifiers() -> None:
    jql = build_search_jql(("", "real-bmn", ""))
    assert jql is not None
    assert "real-bmn" in jql


def test_build_search_jql_escapes_embedded_double_quotes() -> None:
    jql = build_search_jql(('weird"id',))
    assert jql is not None
    assert r"weird\"id" in jql


# --------------------------- resolve_drive_inspect --------------------------


def test_resolve_keeps_action_when_no_existing_ticket_and_no_error() -> None:
    action = _drive_inspect_action()
    targets = {action.bmn: _target()}

    resolved, resolutions = resolve_drive_inspect(
        [action],
        targets,
        search_fn=lambda _ids: (None, None),
    )

    assert resolved == [action]  # action passes through unchanged
    assert resolutions == []  # no resolution rendered


def test_resolve_downgrades_to_noop_when_existing_ticket_found() -> None:
    action = _drive_inspect_action(bmn="bmn-x")
    targets = {action.bmn: _target(bmn="bmn-x")}

    resolved, resolutions = resolve_drive_inspect(
        [action],
        targets,
        search_fn=lambda _ids: ("DO-12345", None),
    )

    (got,) = resolved
    assert got.kind is ActionKind.NOOP
    assert got.command is None
    assert "DO-12345" in got.notes
    assert resolutions == [
        DriveInspectResolution(bmn="bmn-x", existing_ticket="DO-12345", error=None)
    ]


def test_resolve_downgrades_to_noop_when_jira_query_errors() -> None:
    """Defensive: a failed JIRA query is treated like an existing ticket
    so we don't risk creating a duplicate when JIRA is flaky."""
    action = _drive_inspect_action(bmn="bmn-y")
    targets = {action.bmn: _target(bmn="bmn-y")}

    resolved, resolutions = resolve_drive_inspect(
        [action],
        targets,
        search_fn=lambda _ids: (None, "HTTP 401 Unauthorized"),
    )

    (got,) = resolved
    assert got.kind is ActionKind.NOOP
    assert got.command is None
    assert "HTTP 401" in got.notes
    assert "Skipping new ticket creation" in got.notes
    assert resolutions == [
        DriveInspectResolution(bmn="bmn-y", existing_ticket=None, error="HTTP 401 Unauthorized")
    ]


def test_resolve_passes_through_non_drive_inspect_actions() -> None:
    """The resolver only touches DRIVE_INSPECT actions; everything else
    flows through verbatim so we don't accidentally clobber power-drain
    or HO-ticket entries in the same plan."""
    power_drain = PlannedAction(
        bmn="bmn-pd",
        cw_node="g-pd",
        sku="GPU-GH200-01",
        kind=ActionKind.POWER_DRAIN,
        triggering_codes=("CW0211",),
        command="cwctl power-drain ...",
        notes="...",
    )
    targets = {"bmn-pd": _target(bmn="bmn-pd")}

    resolved, resolutions = resolve_drive_inspect(
        [power_drain],
        targets,
        search_fn=lambda _ids: (None, None),
    )

    assert resolved == [power_drain]
    assert resolutions == []


def test_resolve_uses_target_search_identifiers_when_available() -> None:
    """Resolver should expand bmn/cw-node/serial via search_identifiers
    so the DO query has the best chance of matching ticket summaries."""
    action = _drive_inspect_action(bmn="bmn-1", cw_node="g-1")
    target = _target(bmn="bmn-1", cw_node="g-1", serial="S1")
    seen: list[tuple[str, ...]] = []

    def _search(ids: tuple[str, ...]) -> tuple[None, None]:
        seen.append(ids)
        return None, None

    resolve_drive_inspect([action], {action.bmn: target}, search_fn=_search)
    # All three identifiers from the target should reach the search closure.
    assert seen == [("bmn-1", "g-1", "S1")]


def test_resolve_falls_back_to_bmn_only_when_target_missing() -> None:
    """If a target isn't in the lookup dict for some reason, the resolver
    should still query JIRA — using just the BMN name — rather than skip."""
    action = _drive_inspect_action(bmn="orphan-bmn")
    seen: list[tuple[str, ...]] = []

    def _search(ids: tuple[str, ...]) -> tuple[None, None]:
        seen.append(ids)
        return None, None

    resolve_drive_inspect([action], {}, search_fn=_search)
    assert seen == [("orphan-bmn",)]
