"""IBP reseat DCT ticket workflow.

Structurally combines two existing patterns:

  - From `frops.xid109`: a state-machine + HO-description filter (CWNC-STATE
    `triage` + PhaseState reason `nlcc` + HO ticket whose description
    mentions ibp issues).
  - From `frops.drive_inspect`: a DO project dedup that prevents duplicate
    cwctl ticket creation when an open DO ticket about ibp/reseat/fiber
    already exists for the node.

Surviving candidates are transformed to `ActionKind.IBP_RESEAT` with a
pre-rendered `cwctl ticket dct-action network <SERIAL> -m "..." -r <ZONE>`
command. The specific ibp interface (e.g. `ibp2`) is extracted from the
HO description when present so the message reads "ibp2 down. Please
reseat and clean ibp2 …"; falls back to a generic `ibp` label when the
description references ibp without a number (or only via the
`IBMultipleFlaps` alert reason).

DO dedup uses `statusCategory != "Done"` so previously-closed reseat
tickets don't suppress a new flap. That's different from
`frops.drive_inspect`, which deliberately includes closed tickets.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from frops.action import ActionKind, BMNTarget, PlannedAction

# JIRA project for DCT (Data Center Tech) tickets — same pool used by
# `frops.drive_inspect` but the dedup criteria differ (see below).
DO_PROJECT: str = "DO"
# JQL fragment that excludes tickets in the "Done" statusCategory
# (closed / resolved / cancelled). Per spec we only dedup against
# open ibp work so a previously-closed reseat doesn't mask a new flap.
DO_OPEN_JQL: str = 'statusCategory != "Done"'

# Keywords that classify a DO ticket as "ibp work" for dedup purposes.
# JIRA's `~` operator stems, so "fiber"/"fibers", "reseat"/"reseating"
# all match. ANDed against the identifier clause so unrelated DO tickets
# for the same node don't suppress a real ibp flap.
IBP_TICKET_KEYWORDS: tuple[str, ...] = ("ibp", "reseat", "fiber")

# Recognize ibp mentions in HO descriptions. `\bibp\d*\b` catches both
# generic `ibp` (no number) and specific `ibp2`-style references; the
# `IBMultipleFlaps` alternative catches Alertmanager rule names where
# the literal `ibp` substring isn't present in the surrounding prose.
HO_IBP_PATTERN: re.Pattern[str] = re.compile(r"(?i)\bibp\d*\b|\bIBMultipleFlaps\b")
# Captures just the digit suffix when present so we can render the
# specific interface in the cwctl message instead of a generic `ibp`.
HO_IBP_NUMBER_PATTERN: re.Pattern[str] = re.compile(r"(?i)\bibp(\d+)\b")

TRIAGE_STATE: str = "triage"  # bmns -o wide CWNC-STATE
NLCC_OWNS_REASON: str = "nlcc"  # kubectl PhaseState.reason — same heuristic as xid109

GENERIC_IBP_LABEL: str = "ibp"


@dataclass(frozen=True)
class IBPReseatCandidate:
    """Per-BMN outcome from the IBP pipeline.

    The CLI uses `actionable` to decide whether to transform the
    `PlannedAction` into `IBP_RESEAT` (file the ticket) or downgrade to
    NOOP with a skip note. `existing_do_ticket` / `error` carry the
    reason for skipping so the operator can chase the existing ticket
    or fix the JIRA outage.
    """

    bmn: str
    cw_node: str
    sku: str
    serial: str
    region: str
    phase_reason: str
    ho_ticket: str
    ibp_label: str  # "ibp1" / "ibp2" / "ibp" (generic)
    existing_do_ticket: str | None
    error: str | None

    @property
    def actionable(self) -> bool:
        """True when both `existing_do_ticket` and `error` are None."""
        return self.existing_do_ticket is None and self.error is None


# ── Injectable IO type aliases ──────────────────────────────────────────────

# (identifiers) -> first matching HO issue key or None. Mirrors the closure
# the CLI already builds for XID-109 / HO-ticket resolution.
JIRASearchFn = Callable[[tuple[str, ...]], str | None]
# (issue_key) -> description text (or "" when missing).
FetchDescriptionFn = Callable[[str], str]
# (bmn) -> PhaseState reason from kubectl jsonpath (or "" on error).
FetchPhaseFn = Callable[[str], str]
# (identifiers) -> (issue_key|None, error|None) for the DO project lookup.
# The error channel lets the resolver distinguish "no match" (proceed
# with ticket creation) from "JIRA failed" (defensive skip).
DOSearchFn = Callable[[tuple[str, ...]], tuple[str | None, str | None]]


# ── Public helpers ──────────────────────────────────────────────────────────


def description_mentions_ibp(description: str) -> bool:
    """True if the HO description references ibp or the IBMultipleFlaps alert."""
    if not description:
        return False
    return HO_IBP_PATTERN.search(description) is not None


def find_ibp_label_in_description(description: str) -> str:
    """Return e.g. `ibp2` for the first numbered ibp; falls back to `ibp`.

    The cwctl message renders this verbatim ("ibp2 down. Please reseat
    and clean ibp2 …"). When the description only says `ibp` without a
    number, the generic label keeps the message grammatically clean.
    """
    if description:
        match = HO_IBP_NUMBER_PATTERN.search(description)
        if match:
            return f"ibp{match.group(1)}"
    return GENERIC_IBP_LABEL


def ibp_reseat_command(cw_node: str, serial: str, region: str, ibp_label: str) -> str:
    """Render the cwctl ticket creation for one onsite ibp reseat request."""
    message = (
        f"{ibp_label} down. Please reseat and clean {ibp_label} on node "
        f"({cw_node} | SN: {serial}) side."
    )
    return f'cwctl ticket dct-action network {serial} -m "{message}" -r {region}'


def build_do_search_jql(identifiers: tuple[str, ...]) -> str | None:
    """JQL for finding OPEN DO tickets about ibp/reseat work for the node.

    Returns None when no usable identifiers are present (defensive: avoids
    running an unbounded `project = DO` query). Empty identifiers dropped.
    """
    cleaned = [i for i in identifiers if i]
    if not cleaned:
        return None
    id_clauses = " OR ".join(
        f'summary ~ "{_escape(i)}" OR description ~ "{_escape(i)}"' for i in cleaned
    )
    keyword_clauses = " OR ".join(
        f'summary ~ "{kw}" OR description ~ "{kw}"' for kw in IBP_TICKET_KEYWORDS
    )
    return f"project = {DO_PROJECT} AND {DO_OPEN_JQL} AND ({id_clauses}) AND ({keyword_clauses})"


def _escape(value: str) -> str:
    """JQL text-value escape (backslash + double-quote)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ── Pipeline ────────────────────────────────────────────────────────────────


def collect_ibp_reseat_candidates(
    targets: list[BMNTarget],
    cwnc_states: dict[str, str],
    ho_search: JIRASearchFn,
    fetch_description: FetchDescriptionFn,
    fetch_phase: FetchPhaseFn,
    do_search: DOSearchFn,
) -> list[IBPReseatCandidate]:
    """Four-stage pipeline, cheapest filter first.

    For each BMN:
      1. CWNC-STATE must be `triage` (cheap dict lookup).
      2. JIRA HO project must return a match (one network call).
      3. The matched HO ticket's description must mention ibp (one
         network call to fetch the description).
      4. PhaseState reason must be `nlcc` (one kubectl call).

    Only BMNs that survive all four stages run the DO ticket dedup
    (one more network call). Phase reason is queried last so the
    expensive kubectl call only runs for BMNs already known to be
    ibp-relevant.
    """
    candidates: list[IBPReseatCandidate] = []
    for target in targets:
        if cwnc_states.get(target.bmn) != TRIAGE_STATE:
            continue
        identifiers = target.search_identifiers or (target.bmn,)
        ho_key = ho_search(identifiers)
        if not ho_key:
            continue
        description = fetch_description(ho_key)
        if not description_mentions_ibp(description):
            continue
        phase_reason = fetch_phase(target.bmn)
        if phase_reason != NLCC_OWNS_REASON:
            # Still in flcc-only triage; not yet ready for an ibp reseat
            # ticket. Skip silently — when the operator re-runs after the
            # phase progresses, the BMN will surface.
            continue
        ibp_label = find_ibp_label_in_description(description)
        existing, error = do_search(identifiers)
        candidates.append(
            IBPReseatCandidate(
                bmn=target.bmn,
                cw_node=target.cw_node,
                sku=target.sku,
                serial=target.serial,
                region=target.region,
                phase_reason=phase_reason,
                ho_ticket=ho_key,
                ibp_label=ibp_label,
                existing_do_ticket=existing,
                error=error,
            )
        )
    return candidates


def apply_ibp_reseat_overrides(
    actions: list[PlannedAction],
    candidates: list[IBPReseatCandidate],
) -> list[PlannedAction]:
    """Rewrite matching actions in-place (returning a new list).

    Actions whose `kind` is already higher-precedence (POWER_DRAIN,
    DRIVE_INSPECT, XID_109_RETURN_TO_READY) are never reclassified —
    those workflows take priority. HO_TICKET and NOOP can become
    IBP_RESEAT (when actionable) or a noted NOOP (when skipped).
    """
    by_bmn = {c.bmn: c for c in candidates}
    rewritten: list[PlannedAction] = []
    for action in actions:
        cand = by_bmn.get(action.bmn)
        if cand is None or action.kind in (
            ActionKind.POWER_DRAIN,
            ActionKind.DRIVE_INSPECT,
            ActionKind.XID_109_RETURN_TO_READY,
        ):
            rewritten.append(action)
            continue

        if cand.actionable:
            rewritten.append(
                PlannedAction(
                    bmn=cand.bmn,
                    cw_node=cand.cw_node,
                    sku=cand.sku,
                    kind=ActionKind.IBP_RESEAT,
                    triggering_codes=action.triggering_codes,
                    command=ibp_reseat_command(
                        cw_node=cand.cw_node,
                        serial=cand.serial,
                        region=cand.region,
                        ibp_label=cand.ibp_label,
                    ),
                    notes=(
                        f"HO ticket {cand.ho_ticket} mentions {cand.ibp_label}; "
                        "CWNC-STATE=triage; phase=nlcc → file new DO ticket "
                        "for onsite reseat/clean."
                    ),
                    jira_issue=None,
                )
            )
        else:
            # Existing DO ticket OR JIRA query failed; downgrade to NOOP
            # so we don't double-file. The skip reason flows into the
            # notes so the operator can chase the existing ticket or
            # fix JIRA auth.
            if cand.existing_do_ticket is not None:
                note = (
                    f"HO ticket {cand.ho_ticket} mentions {cand.ibp_label}; "
                    f"open DO ticket {cand.existing_do_ticket} already covers "
                    "reseat/ibp work for this node — skipping new ticket."
                )
            else:
                note = (
                    f"HO ticket {cand.ho_ticket} mentions {cand.ibp_label}; "
                    "could not query the DO project for an existing reseat "
                    f"ticket ({cand.error}). Skipping to avoid duplicates."
                )
            rewritten.append(
                PlannedAction(
                    bmn=cand.bmn,
                    cw_node=cand.cw_node,
                    sku=cand.sku,
                    kind=ActionKind.NOOP,
                    triggering_codes=action.triggering_codes,
                    command=None,
                    notes=note,
                )
            )
    return rewritten


def render_ibp_skipped_summary(candidates: list[IBPReseatCandidate]) -> str:
    """Info block listing IBP candidates whose new DO ticket was suppressed.

    Empty when nothing was skipped. Surfaces the existing DO ticket key
    (so the operator can chase it) or the JIRA error string (so they
    can fix auth / connectivity).
    """
    skipped = [c for c in candidates if not c.actionable]
    if not skipped:
        return ""

    lines: list[str] = [
        f"=== IBP reseat candidates skipped ({len(skipped)}) ===",
        "",
        "These BMNs match an HO ticket mentioning ibp + reached `nlcc`",
        "phase from triage, but a new DO ticket won't be filed because",
        "the DO project either has an open ibp ticket already or",
        "couldn't be queried.",
        "",
    ]
    for c in skipped:
        target_str = (
            c.existing_do_ticket if c.existing_do_ticket is not None else f"(skipped: {c.error})"
        )
        lines.append(f"  - {c.bmn} ({c.ibp_label}, HO: {c.ho_ticket})  →  {target_str}")
    return "\n".join(lines)
