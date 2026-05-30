"""Classify BMNs by detected CW codes and build a per-BMN action plan.

Phase A: this module only builds and renders a plan — it does *not* execute
cwctl, talk to JIRA, or otherwise mutate state. Phase B will introduce
execution behind `--yes`, and Phase C will summarize actioned nodes via the
Anthropic API. Keeping classification pure makes the policy easy to test
in isolation from kubectl/awxstat/cwctl.

Policy mapping (see CONTRIBUTING for the source-of-truth recipe):

- CW0211 or CW0102 on SKU GPU-GH200-01 → power-drain via cwctl
- CW0201                              → JIRA HO ticket (search → update or
                                        create via return-to-triage)
- anything else                       → no-op

Power-drain takes precedence over HO-ticket when both code groups are
present on the same BMN — the simpler remediation runs first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from frops.awx import AWXReport, CWError


class ActionKind(str, Enum):
    POWER_DRAIN = "power-drain"
    HO_TICKET = "ho-ticket"
    NOOP = "noop"


POWER_DRAIN_CODES: frozenset[str] = frozenset({"CW0211", "CW0102"})
POWER_DRAIN_ELIGIBLE_SKUS: frozenset[str] = frozenset({"GPU-GH200-01"})
HO_TICKET_CODES: frozenset[str] = frozenset({"CW0201"})


@dataclass(frozen=True)
class BMNTarget:
    """A BMN row from the SKU view joined with its AWX reports.

    The CLI builds these from the kubectl JSON output plus two awxstat
    invocations (mgmt + bmc). Targets without a CW-NODE name are filtered
    out before this struct is created.
    """

    bmn: str
    cw_node: str
    sku: str
    awx_reports: tuple[AWXReport, ...]

    @property
    def detected_codes(self) -> tuple[CWError, ...]:
        codes: list[CWError] = []
        for report in self.awx_reports:
            codes.extend(report.cw_error_codes)
        return tuple(codes)


@dataclass(frozen=True)
class PlannedAction:
    bmn: str
    cw_node: str
    sku: str
    kind: ActionKind
    triggering_codes: tuple[str, ...]
    command: str | None
    notes: str


def _power_drain_command(bmn: str, code: str) -> str:
    return (
        f"cwctl flcc node --one-off -w orphan -s power-drain {bmn} "
        f'-o -m "power draining node to clear {code}"'
    )


def _return_to_triage_command(bmn: str) -> str:
    return (
        f"cwctl flcc node -w return-to-triage {bmn} "
        '-o -m "Sending node to triage to generate HO ticket. Sending node to RMA"'
    )


def classify(target: BMNTarget) -> PlannedAction:
    """Return the planned action for one BMN target.

    Power-drain wins over HO-ticket; "no actionable codes" wins when neither
    rule matches. The exact cwctl invocation is rendered now (string only —
    nothing is executed) so Phase B can lift it verbatim.
    """
    codes_present = {err.code for err in target.detected_codes}

    if target.sku in POWER_DRAIN_ELIGIBLE_SKUS:
        matching = sorted(codes_present & POWER_DRAIN_CODES)
        if matching:
            code = matching[0]
            return PlannedAction(
                bmn=target.bmn,
                cw_node=target.cw_node,
                sku=target.sku,
                kind=ActionKind.POWER_DRAIN,
                triggering_codes=tuple(matching),
                command=_power_drain_command(target.bmn, code),
                notes=f"Detected {', '.join(matching)} on a {target.sku} node",
            )

    if codes_present & HO_TICKET_CODES:
        matching = sorted(codes_present & HO_TICKET_CODES)
        return PlannedAction(
            bmn=target.bmn,
            cw_node=target.cw_node,
            sku=target.sku,
            kind=ActionKind.HO_TICKET,
            triggering_codes=tuple(matching),
            # Phase B will resolve to either a JIRA update or this fallback.
            command=_return_to_triage_command(target.bmn),
            notes=(
                "Would search JIRA for an HO ticket in 'Awaiting Support'; "
                "if found, update Description (Phase B). Otherwise run the "
                "return-to-triage workflow shown above."
            ),
        )

    if not codes_present:
        notes = "No CW codes detected in AWX output"
    else:
        notes = f"Codes {sorted(codes_present)} are not actionable for SKU {target.sku}"

    return PlannedAction(
        bmn=target.bmn,
        cw_node=target.cw_node,
        sku=target.sku,
        kind=ActionKind.NOOP,
        triggering_codes=tuple(sorted(codes_present)),
        command=None,
        notes=notes,
    )


def render_plan(actions: list[PlannedAction]) -> str:
    """Render a human-friendly summary of planned actions."""
    if not actions:
        return "=== Planned actions ===\n\n(no eligible BMNs found)"

    by_kind: dict[ActionKind, list[PlannedAction]] = {}
    for action in actions:
        by_kind.setdefault(action.kind, []).append(action)

    lines: list[str] = ["=== Planned actions ==="]

    for kind in (ActionKind.POWER_DRAIN, ActionKind.HO_TICKET, ActionKind.NOOP):
        bucket = by_kind.get(kind, [])
        if not bucket:
            continue
        lines.append(f"\n[{kind.value}] {len(bucket)} node(s)")
        for action in bucket:
            lines.append(f"  - {action.bmn} (CW-NODE={action.cw_node}, SKU={action.sku})")
            if action.triggering_codes:
                lines.append(f"      codes: {', '.join(action.triggering_codes)}")
            if action.command:
                lines.append(f"      command: {action.command}")
            lines.append(f"      note: {action.notes}")

    totals = ", ".join(f"{kind.value}={len(by_kind.get(kind, []))}" for kind in ActionKind)
    lines.append(f"\nTotals: {totals}")
    return "\n".join(lines)
