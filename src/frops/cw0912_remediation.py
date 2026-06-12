"""CW0912 stage-2/3 state machine: tray-reseat escalation and recovery cleanup.

This module owns the *cross-run* CW0912 logic that needs the persistent
state file from `frops.cw0912_state`:

  - **stage 2** — When CW0912 reappears in a *different* AWX job after a
    successful stage-1 power-drain, file a DCT tray-reseat ticket
    (unless one is already open for this node).
  - **stage 3 (placeholder)** — Third occurrence after the tray reseat
    is logged here but not actioned in this PR; phase 3 will add the
    HO RMA escalation.
  - **recovery cleanup** — When a BMN no longer reports CW0912 in its
    current AWX job, clear its state file so a future flap starts at
    stage 1 again.

Stage 1 (the initial power-drain + scheduled node-zap rerun) is owned
by `frops.action.classify` + `frops.cw0912`; this module overrides that
classification when prior state indicates we already remediated.

Pure-functional: every IO surface (state read/write/clear, JIRA DO
search, current-time) is passed in as a callable so tests can drive the
full state machine without disk or network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from frops.action import ActionKind, BMNTarget, PlannedAction
from frops.cw0912_state import (
    STAGE_POWER_DRAIN_SCHEDULED,
    STAGE_RMA_ESCALATED,
    STAGE_TRAY_RESEAT_FILED,
    CW0912State,
)

CW0912: str = "CW0912"
GH200_SKU: str = "GPU-GH200-01"

# JIRA project + keywords for the tray-reseat dedup. Mirrors the
# drive_inspect / ibp_reseat shape, but the keyword set is GPU-tray-
# specific so an unrelated network reseat ticket won't suppress this.
DO_PROJECT: str = "DO"
TRAY_RESEAT_KEYWORDS: tuple[str, ...] = ("gpu tray", "tray reseat", "reseat the gpu")


class CW0912Stage(str, Enum):
    """What `apply_cw0912_overrides` should do for a given candidate.

    `FIRST_OCCURRENCE` leaves the POWER_DRAIN action alone — stage 1
    plumbing (frops.cw0912) handles the at-scheduled node-zap rerun.
    """

    FIRST_OCCURRENCE = "first_occurrence"
    IN_PROGRESS = "in_progress"  # same AWX job, already remediated — skip
    SECOND_OCCURRENCE = "second_occurrence"  # file tray reseat
    THIRD_OCCURRENCE = "third_occurrence"  # phase 3 (RMA) — note only for now


@dataclass(frozen=True)
class CW0912Candidate:
    """One BMN's CW0912 stage decision + the inputs that drove it."""

    bmn: str
    cw_node: str
    sku: str
    serial: str
    region: str
    current_job_id: str
    stage: CW0912Stage
    prior_state: CW0912State | None
    existing_do_ticket: str | None
    do_search_error: str | None

    @property
    def actionable(self) -> bool:
        """True only for SECOND_OCCURRENCE without a DO dedup hit or JIRA error."""
        return (
            self.stage is CW0912Stage.SECOND_OCCURRENCE
            and self.existing_do_ticket is None
            and self.do_search_error is None
        )


# --- Type aliases for injectable IO ------------------------------------------

StateReader = Callable[[str], CW0912State | None]
StateWriter = Callable[[CW0912State], None]
StateClearer = Callable[[str], None]
# `do_search(identifiers) -> (matching_key | None, error | None)` — mirrors
# the closure shape `cli.py` already builds for drive_inspect / ibp_reseat.
DOSearchFn = Callable[[tuple[str, ...]], tuple[str | None, str | None]]
NowFn = Callable[[], str]  # returns ISO-8601 UTC


# --- Command rendering -------------------------------------------------------


def tray_reseat_command(bmn: str, cw_node: str, serial: str, region: str) -> str:
    """Render the cwctl invocation for the onsite GPU tray reseat ticket.

    Mirrors the operator-documented form:
      cwctl ticket dct-action device <BMN>
            -m "Please reseat the GPU tray on node (<CWNODE> | SN: <SERIAL>)"
            -r <REGION>
    """
    message = f"Please reseat the GPU tray on node ({cw_node} | SN: {serial})"
    return f'cwctl ticket dct-action device {bmn} -m "{message}" -r {region}'


def _escape(value: str) -> str:
    """JQL text-value escape — backslash + double-quote."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_tray_reseat_search_jql(identifiers: tuple[str, ...]) -> str | None:
    """JQL for finding any DO ticket about a GPU tray reseat on this BMN.

    Includes both open AND closed tickets (matches drive_inspect's
    "don't recreate even if closed" stance) — a closed reseat means
    the work was done; if CW0912 came back, we shouldn't file another.
    Stage 3 (RMA escalation) catches that case separately.
    Returns None when no usable identifiers are present.
    """
    cleaned = [i for i in identifiers if i]
    if not cleaned:
        return None
    id_clauses = " OR ".join(
        f'summary ~ "{_escape(i)}" OR description ~ "{_escape(i)}"' for i in cleaned
    )
    keyword_clauses = " OR ".join(
        f'summary ~ "{kw}" OR description ~ "{kw}"' for kw in TRAY_RESEAT_KEYWORDS
    )
    # `DO` is a reserved JQL word — must be quoted.
    return f'project = "{DO_PROJECT}" AND ({id_clauses}) AND ({keyword_clauses})'


# --- Stage decision ----------------------------------------------------------


def _decide_stage(prior: CW0912State | None, current_job_id: str) -> CW0912Stage:
    """Map (prior state, current AWX job_id) → CW0912Stage.

    Decision table:
      no prior state                                 → FIRST_OCCURRENCE
      prior.job_id == current_job_id                 → IN_PROGRESS (skip)
      prior.stage == POWER_DRAIN_SCHEDULED + new job → SECOND_OCCURRENCE
      prior.stage == TRAY_RESEAT_FILED   + new job   → THIRD_OCCURRENCE
      prior.stage == RMA_ESCALATED                   → THIRD_OCCURRENCE (steady-state)
      any other prior.stage                          → FIRST_OCCURRENCE (defensive)
    """
    if prior is None:
        return CW0912Stage.FIRST_OCCURRENCE
    if prior.job_id == current_job_id:
        return CW0912Stage.IN_PROGRESS
    if prior.stage == STAGE_POWER_DRAIN_SCHEDULED:
        return CW0912Stage.SECOND_OCCURRENCE
    if prior.stage in (STAGE_TRAY_RESEAT_FILED, STAGE_RMA_ESCALATED):
        return CW0912Stage.THIRD_OCCURRENCE
    # Corrupt / unknown stage — fall back to first-occurrence so the
    # operator at least gets the safe baseline.
    return CW0912Stage.FIRST_OCCURRENCE


def collect_cw0912_candidates(
    targets: list[BMNTarget],
    *,
    state_reader: StateReader,
    do_search: DOSearchFn,
) -> list[CW0912Candidate]:
    """For every GH200 BMN with CW0912 in AWX, decide its stage.

    Non-GH200 SKUs and BMNs without CW0912 in the current AWX output
    are skipped (the recovery cleanup pass handles state-file teardown
    for those). DO project dedup is only run for SECOND_OCCURRENCE
    candidates — no point spending a JIRA call on the others.
    """
    candidates: list[CW0912Candidate] = []
    for target in targets:
        if target.sku != GH200_SKU:
            continue
        current_job_id = target.first_job_id_for_code(CW0912)
        if current_job_id is None:
            continue  # no CW0912 in this BMN's AWX output

        prior = state_reader(target.bmn)
        stage = _decide_stage(prior, current_job_id)

        existing: str | None = None
        error: str | None = None
        if stage is CW0912Stage.SECOND_OCCURRENCE:
            jql_ok = build_tray_reseat_search_jql(target.search_identifiers or (target.bmn,))
            if jql_ok is None:
                error = "no usable identifiers for DO search"
            else:
                existing, error = do_search(target.search_identifiers or (target.bmn,))

        candidates.append(
            CW0912Candidate(
                bmn=target.bmn,
                cw_node=target.cw_node,
                sku=target.sku,
                serial=target.serial,
                region=target.region,
                current_job_id=current_job_id,
                stage=stage,
                prior_state=prior,
                existing_do_ticket=existing,
                do_search_error=error,
            )
        )
    return candidates


# --- Override application ---------------------------------------------------


def _to_noop(action: PlannedAction, *, notes: str) -> PlannedAction:
    """Downgrade `action` to NOOP with an explanatory note + no command."""
    return PlannedAction(
        bmn=action.bmn,
        cw_node=action.cw_node,
        sku=action.sku,
        kind=ActionKind.NOOP,
        triggering_codes=action.triggering_codes,
        command=None,
        notes=notes,
    )


def apply_cw0912_overrides(
    actions: list[PlannedAction],
    candidates: list[CW0912Candidate],
) -> list[PlannedAction]:
    """Rewrite POWER_DRAIN actions per the CW0912 stage decisions.

    Only POWER_DRAIN actions (the classification CW0912 lands on by
    default) are eligible for override — POWER_DRAIN_DRIVE_INSPECT,
    XID_109_RETURN_TO_READY, etc. are higher-precedence pipelines and
    we don't touch them.
    """
    by_bmn = {c.bmn: c for c in candidates}
    out: list[PlannedAction] = []
    for action in actions:
        cand = by_bmn.get(action.bmn)
        if cand is None or action.kind is not ActionKind.POWER_DRAIN:
            out.append(action)
            continue

        if cand.stage is CW0912Stage.FIRST_OCCURRENCE:
            # Stage-1 path — leave the existing POWER_DRAIN untouched.
            out.append(action)
            continue

        if cand.stage is CW0912Stage.IN_PROGRESS:
            out.append(
                _to_noop(
                    action,
                    notes=(
                        f"CW0912 remediation already in progress for AWX job "
                        f"{cand.current_job_id} — wait for the scheduled node-zap "
                        "rerun to fire before re-running this command."
                    ),
                )
            )
            continue

        if cand.stage is CW0912Stage.SECOND_OCCURRENCE:
            if cand.actionable:
                out.append(
                    PlannedAction(
                        bmn=cand.bmn,
                        cw_node=cand.cw_node,
                        sku=action.sku,
                        kind=ActionKind.CW0912_TRAY_RESEAT,
                        triggering_codes=action.triggering_codes,
                        command=tray_reseat_command(
                            bmn=cand.bmn,
                            cw_node=cand.cw_node,
                            serial=cand.serial,
                            region=cand.region,
                        ),
                        notes=(
                            f"CW0912 returned in AWX job {cand.current_job_id} after "
                            f"prior remediation (job {cand.prior_state.job_id if cand.prior_state else '?'}) — "
                            "filing onsite GPU tray reseat via DCT."
                        ),
                    )
                )
            else:
                reason = (
                    f"existing DO tray-reseat ticket {cand.existing_do_ticket} blocks duplicate"
                    if cand.existing_do_ticket
                    else f"JIRA DO search failed: {cand.do_search_error}"
                )
                out.append(
                    _to_noop(
                        action,
                        notes=f"CW0912 second occurrence — skipping tray-reseat ({reason}).",
                    )
                )
            continue

        if cand.stage is CW0912Stage.THIRD_OCCURRENCE:
            out.append(
                _to_noop(
                    action,
                    notes=(
                        "CW0912 third+ occurrence — tray reseat did not resolve. "
                        "RMA escalation is the next step (phase 3, not yet implemented). "
                        "Update the HO ticket manually for RMA processing."
                    ),
                )
            )
    return out


# --- Post-execution state writes --------------------------------------------


def persist_cw0912_state_after_execute(
    results: list[tuple[PlannedAction, int]],
    candidates: list[CW0912Candidate],
    *,
    state_writer: StateWriter,
    now: NowFn,
) -> list[CW0912State]:
    """After Phase B, write fresh state for each successful CW0912 action.

    `results` is `(action, rc)` pairs — the caller flattens an
    `ExecutionSummary` into this shape. Returns the list of written
    states for caller logging.

    Mapping from successful action → new stage:
      POWER_DRAIN with CW0912 in triggering codes → POWER_DRAIN_SCHEDULED
      CW0912_TRAY_RESEAT                          → TRAY_RESEAT_FILED
    """
    by_bmn = {c.bmn: c for c in candidates}
    written: list[CW0912State] = []
    for action, rc in results:
        if rc != 0:
            continue
        cand = by_bmn.get(action.bmn)
        if cand is None:
            continue
        if action.kind is ActionKind.POWER_DRAIN and CW0912 in action.triggering_codes:
            state = CW0912State(
                bmn=cand.bmn,
                job_id=cand.current_job_id,
                observed_at=now(),
                stage=STAGE_POWER_DRAIN_SCHEDULED,
            )
        elif action.kind is ActionKind.CW0912_TRAY_RESEAT:
            state = CW0912State(
                bmn=cand.bmn,
                job_id=cand.current_job_id,
                observed_at=now(),
                stage=STAGE_TRAY_RESEAT_FILED,
            )
        else:
            continue
        state_writer(state)
        written.append(state)
    return written


# --- Recovery cleanup -------------------------------------------------------


def clear_recovered_cw0912_states(
    targets: list[BMNTarget],
    *,
    state_reader: StateReader,
    state_clearer: StateClearer,
) -> list[str]:
    """For BMNs whose current AWX output no longer carries CW0912, drop state.

    Returns the list of BMN names cleared so the CLI can surface a
    one-line "recovered N BMNs" note. Idempotent — clearing a missing
    state file is a no-op.
    """
    cleared: list[str] = []
    for target in targets:
        if target.sku != GH200_SKU:
            continue
        if any(err.code == CW0912 for err in target.detected_codes):
            continue
        if state_reader(target.bmn) is None:
            continue
        state_clearer(target.bmn)
        cleared.append(target.bmn)
    return cleared


# --- Plan-side summary ------------------------------------------------------


def render_cw0912_skipped_summary(candidates: list[CW0912Candidate]) -> str:
    """Info block for CW0912 candidates the override didn't act on.

    Surfaces IN_PROGRESS, dedup-skipped SECOND_OCCURRENCE, and
    THIRD_OCCURRENCE candidates so the operator sees them without
    scanning the planned-actions block. Empty input → "".
    """
    skipped = [
        c for c in candidates if c.stage is not CW0912Stage.FIRST_OCCURRENCE and not c.actionable
    ]
    if not skipped:
        return ""

    lines: list[str] = [
        f"=== CW0912 candidates not actioned this pass ({len(skipped)}) ===",
        "",
        "Each row explains why a CW0912 BMN was skipped — either we already",
        "remediated for the current AWX job, an existing DO tray-reseat",
        "ticket blocks a duplicate, or the BMN has escalated past where",
        "stage 2 can help (RMA territory).",
        "",
    ]

    cols: list[tuple[str, list[str]]] = [
        ("BMN", [c.bmn for c in skipped]),
        ("CW-NODE", [c.cw_node or "(none)" for c in skipped]),
        ("STAGE", [c.stage.value for c in skipped]),
        ("JOB ID", [c.current_job_id for c in skipped]),
        ("NOTE", [_skip_note(c) for c in skipped]),
    ]
    widths = [max(len(h), max(len(v) for v in vs)) for h, vs in cols]
    lines.append("  ".join(h.ljust(w) for (h, _), w in zip(cols, widths, strict=True)))
    lines.append("  ".join("-" * w for w in widths))
    for i in range(len(skipped)):
        row = [vs[i] for _, vs in cols]
        cells = [v.ljust(w) for v, w in zip(row, widths, strict=True)]
        lines.append("  ".join(cells))
    return "\n".join(lines)


def _skip_note(c: CW0912Candidate) -> str:
    if c.stage is CW0912Stage.IN_PROGRESS:
        return "same AWX job — wait for scheduled node-zap rerun"
    if c.stage is CW0912Stage.SECOND_OCCURRENCE and c.existing_do_ticket:
        return f"existing DO ticket {c.existing_do_ticket}"
    if c.stage is CW0912Stage.SECOND_OCCURRENCE and c.do_search_error:
        return f"JIRA error: {c.do_search_error}"
    if c.stage is CW0912Stage.THIRD_OCCURRENCE:
        return "RMA territory (phase 3 — manual HO update for now)"
    return ""
