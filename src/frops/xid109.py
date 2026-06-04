"""XID-109 return-to-ready classifier.

Workflow this module supports:

  1. For each BMN whose `bmns -o wide` CWNC-STATE is `triage`, search the
     JIRA HO project for an open ticket matching the BMN and check the
     description for an XID 109 mention.
  2. Of those matches, split into:

       - **List A (actionable)** — both flcc and nlcc have reached triage.
         Heuristic: `PhaseState` condition's `reason` is `nlcc`, signaling
         nlcc has taken over after flcc. These can run
         `cwctl flcc node -w return-to-ready ...`.
       - **List B (waiting)** — still propagating through return-to-fleetops.
         Any other PhaseState reason. Informational only; can't action yet.

The classifier is purely functional: shell-outs and JIRA calls are passed
in as callables so tests can drive the whole flow without subprocesses or
network. The CLI binds the real callables in cli.py.

If the List A / List B heuristic ever drifts from the real state machine,
the only function to update is `classify_xid109_target`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from frops.action import BMNTarget

# Substring search is case-insensitive and tolerant to the common forms:
#   - "Xid 109"                       (space separator)
#   - "Xid109"                        (no separator; appears in compound
#                                      tokens like GPUContextSwitchTimeoutXid109)
#   - "Xid-109", "Xid_109"            (punctuation separators)
# No leading anchor so we catch the camelCase case ("…TimeoutXid109") — Python
# `\b` is purely word-character-based and the boundary between "t" and "X" is
# not a word boundary. Trailing `\b` prevents matching `xid 1090` / `xid21091`.
XID_109_REGEX: re.Pattern[str] = re.compile(r"(?i)xid[\s_\-]*109\b")

# Map of (cmd) -> (stdout, rc). Matches frops.commands.capture_command's shape
# and the access module's CaptureFn — kwargs let callers thread `timeout=N`
# without breaking the type contract.
CaptureFn = Callable[..., tuple[str, int]]

# Map of (issue_key) -> description text. Bound to JIRAClient.fetch_description
# in the CLI; tests pass a dict-backed fake.
FetchDescriptionFn = Callable[[str], str]

# Map of (identifiers) -> first matching issue key or None. Reuses the same
# search closure cli.py builds for the HO-ticket resolver.
JIRASearchFn = Callable[[tuple[str, ...]], str | None]

TRIAGE_STATE: str = "triage"
NLCC_OWNS_REASON: str = "nlcc"


@dataclass(frozen=True)
class XID109Candidate:
    """One BMN that passed the trigger filters (CWNC-STATE=triage + XID 109 ticket)."""

    bmn: str
    cw_node: str
    sku: str
    cwnc_state: str
    phase_reason: str
    jira_issue: str  # the matching HO ticket key
    # True if both flcc and nlcc have reached triage → eligible for
    # return-to-ready. False means still returning to fleetops (waiting).
    actionable: bool


def description_mentions_xid_109(description: str) -> bool:
    """True if any XID-109 variant appears in the description."""
    if not description:
        return False
    return XID_109_REGEX.search(description) is not None


def parse_cwnc_states(bmns_wide_output: str) -> dict[str, str]:
    """Parse a multi-BMN `bmns -o wide` output into {bmn_name: cwnc_state}.

    The data row split logic mirrors frops.access._extract_ts — kubectl's
    column separator is 2+ whitespace, robust against value overflow into
    the next header position.

    Empty lines and the header row are skipped. Rows that don't have both
    NAME and CWNC-STATE columns parse to ("", "") and are dropped.
    """
    lines = [line for line in bmns_wide_output.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}

    headers = re.findall(r"\S+", lines[0])
    if "NAME" not in headers or "CWNC-STATE" not in headers:
        return {}
    name_idx = headers.index("NAME")
    state_idx = headers.index("CWNC-STATE")

    out: dict[str, str] = {}
    for line in lines[1:]:
        values = re.split(r"\s{2,}", line.strip())
        if max(name_idx, state_idx) >= len(values):
            continue
        name = values[name_idx].strip()
        state = values[state_idx].strip()
        if name:
            out[name] = state
    return out


def fetch_phase_reason(bmn: str, capture: CaptureFn) -> str:
    """Read the `PhaseState` condition's `reason` field for a BMN.

    Returns the bare reason string (e.g. `"flcc"`, `"nlcc"`, `"production"`)
    or "" when kubectl errors / no such condition exists. Callers branch on
    the exact string, so any unexpected value falls through to the "waiting"
    bucket safely.
    """
    jsonpath = "{.status.reportedNodeInfo.status.conditions[?(@.type=='PhaseState')].reason}"
    cmd = f'kubectl get bmn {bmn} -o jsonpath="{jsonpath}"'
    out, rc = capture(cmd)
    if rc != 0:
        return ""
    return out.strip()


def classify_xid109_target(
    target: BMNTarget,
    cwnc_state: str,
    phase_reason: str,
    jira_issue: str,
) -> XID109Candidate:
    """Build the candidate record for a single triage-XID-109 BMN.

    `actionable` flips True only when nlcc has taken over after flcc
    (PhaseState reason == "nlcc"), which we treat as "both controllers
    have reached triage". Any other reason — including the more common
    intermediate values during return-to-fleetops propagation — leaves
    the candidate in the waiting list.
    """
    return XID109Candidate(
        bmn=target.bmn,
        cw_node=target.cw_node,
        sku=target.sku,
        cwnc_state=cwnc_state,
        phase_reason=phase_reason,
        jira_issue=jira_issue,
        actionable=phase_reason == NLCC_OWNS_REASON,
    )


def return_to_ready_command(bmn: str) -> str:
    """Render the cwctl command that returns one BMN to ready state."""
    return (
        f"cwctl flcc node -w return-to-ready {bmn} "
        '-o -m "sending node back to ready, node failed prod for XID 109"'
    )


def collect_xid109_candidates(
    targets: list[BMNTarget],
    cwnc_states: dict[str, str],
    jira_search: JIRASearchFn,
    fetch_description: FetchDescriptionFn,
    fetch_phase: Callable[[str], str],
) -> list[XID109Candidate]:
    """Pipeline: triage filter → JIRA match → description scan → phase read.

    Each stage filters out non-qualifying BMNs before the next (more
    expensive) shell-out runs. `jira_search`, `fetch_description`, and
    `fetch_phase` are passed in for testability; the CLI binds real
    closures around JIRAClient / capture_command.

    A BMN appears in the output iff:
      1. cwnc_states[bmn] == "triage", AND
      2. jira_search returns a non-None HO issue key, AND
      3. fetch_description for that key contains an XID 109 mention.

    For surviving BMNs, fetch_phase is called to fill the PhaseState
    reason, which decides actionable vs waiting.
    """
    candidates: list[XID109Candidate] = []
    for target in targets:
        if cwnc_states.get(target.bmn) != TRIAGE_STATE:
            continue
        identifiers = target.search_identifiers or (target.bmn,)
        issue_key = jira_search(identifiers)
        if not issue_key:
            continue
        if not description_mentions_xid_109(fetch_description(issue_key)):
            continue
        phase_reason = fetch_phase(target.bmn)
        candidates.append(
            classify_xid109_target(
                target=target,
                cwnc_state=TRIAGE_STATE,
                phase_reason=phase_reason,
                jira_issue=issue_key,
            )
        )
    return candidates


def split_by_actionable(
    candidates: list[XID109Candidate],
) -> tuple[list[XID109Candidate], list[XID109Candidate]]:
    """Partition into (List A: actionable, List B: waiting)."""
    list_a = [c for c in candidates if c.actionable]
    list_b = [c for c in candidates if not c.actionable]
    return list_a, list_b


def render_waiting_summary(waiting: list[XID109Candidate]) -> str:
    """Render the "BMNs waiting for return-to-fleetops" section.

    Surfaces JIRA ticket + flcc/nlcc state so the operator can sanity-check
    the classification. Skipped (returns "") when nothing's waiting.
    """
    if not waiting:
        return ""

    lines: list[str] = [
        f"=== XID 109 BMNs waiting for return-to-fleetops ({len(waiting)}) ===",
        "",
        "These BMNs match an open HO ticket mentioning XID 109 but have not",
        "yet reached both flcc-triage and nlcc-triage. They are not actionable",
        "via return-to-ready until propagation completes.",
        "",
    ]

    cols: list[tuple[str, list[str]]] = [
        ("BMN", [c.bmn for c in waiting]),
        ("CW-NODE", [c.cw_node or "(none)" for c in waiting]),
        ("CWNC-STATE", [c.cwnc_state for c in waiting]),
        ("PHASE-REASON", [c.phase_reason or "(unknown)" for c in waiting]),
        ("HO TICKET", [c.jira_issue for c in waiting]),
    ]
    widths = [max(len(h), max(len(v) for v in vs)) for h, vs in cols]
    lines.append("  ".join(h.ljust(w) for (h, _), w in zip(cols, widths, strict=True)))
    lines.append("  ".join("-" * w for w in widths))
    for i in range(len(waiting)):
        row = [vs[i] for _, vs in cols]
        lines.append("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    return "\n".join(lines)
