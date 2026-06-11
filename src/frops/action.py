"""Classify BMNs by detected CW codes and build a per-BMN action plan.

Phase A built the plan (read-only). Phase B (this module's `execute_plan`)
runs the rendered `cwctl` commands for actions with a non-None command,
gated by the CLI's `--yes`/prompt logic. JIRA HO-ticket lookups are *not*
implemented yet — HO actions still fall back to the return-to-triage
command. Phase C will summarize actioned nodes via the Anthropic API.

Classification stays free of subprocess concerns; execution accepts a
`runner` callable so tests can drive it without touching the shell.

Policy mapping (see CONTRIBUTING for the source-of-truth recipe):

- CW0211 or CW0102 on SKU GPU-GH200-01 → power-drain via cwctl
- CW0201                              → JIRA HO ticket (search → update or
                                        create via return-to-triage)
- anything else                       → no-op

Power-drain takes precedence over HO-ticket when both code groups are
present on the same BMN — the simpler remediation runs first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from frops.awx import AWXReport, CWError
from frops.colors import cyan, yellow


class ActionKind(str, Enum):
    POWER_DRAIN = "power-drain"
    HO_TICKET = "ho-ticket"
    # Set by the XID-109 pipeline (frops.xid109) when a BMN has CWNC-STATE=triage,
    # an open HO ticket mentioning XID 109, and PhaseState reason=nlcc. The
    # remediation is `cwctl flcc node -w return-to-ready ...`.
    XID_109_RETURN_TO_READY = "xid-109-return-to-ready"
    # Set when a BMN reports `CW0810: No drives detected` in AWX on an
    # eligible SKU. The remediation is to file a DCT ticket (project DO)
    # requesting onsite drive inspection / install. `frops.drive_inspect`
    # downgrades this to NOOP when an existing DO ticket about drives for
    # the node already exists (open OR closed), so we don't duplicate.
    DRIVE_INSPECT = "drive-inspect"
    # Set by the IBP reseat pipeline (frops.ibp_reseat) when a triage BMN
    # has an HO ticket mentioning ibp issues + PhaseState reason=nlcc + no
    # existing open DO ticket about ibp work. Remediation is a
    # `cwctl ticket dct-action network ...` for onsite reseat/clean.
    IBP_RESEAT = "ibp-reseat"
    NOOP = "noop"


POWER_DRAIN_CODES: frozenset[str] = frozenset({"CW0211", "CW0102", "CW0912"})
POWER_DRAIN_ELIGIBLE_SKUS: frozenset[str] = frozenset({"GPU-GH200-01"})
HO_TICKET_CODES: frozenset[str] = frozenset({"CW0201"})
# CW0810: AWX reports "No drives were detected" — node needs onsite
# inspection or drive install. Eligible on GH200 only for now; add SKUs
# to the frozenset below to extend.
DRIVE_NOT_DETECTED_CODES: frozenset[str] = frozenset({"CW0810"})
DRIVE_INSPECT_ELIGIBLE_SKUS: frozenset[str] = frozenset({"GPU-GH200-01"})


@dataclass(frozen=True)
class BMNTarget:
    """A BMN row from the SKU view joined with its AWX reports.

    The CLI builds these from the kubectl JSON output plus two awxstat
    invocations (mgmt + bmc). Targets without a CW-NODE name are filtered
    out before this struct is created.

    `serial` is the uppercase hardware serial pulled from
    `metadata.labels."ds.coreweave.com/status.asset.serial"`. Empty when
    the label is absent. Used by HO-ticket lookup to match titles like
    `Service Request: RNO2A S484652X4601242`.
    """

    bmn: str
    cw_node: str
    sku: str
    awx_reports: tuple[AWXReport, ...]
    serial: str = ""
    # Pulled from `flcc.coreweave.com/workflow` / `…/workflow-step` / `…/state`
    # labels in the initial kubectl JSON fetch. Empty when the label is absent.
    # Used by the access-check pass to enrich NOOP-no-code diagnostic output.
    workflow: str = ""
    workflow_step: str = ""
    state: str = ""
    # Source of the `-r <REGION>` flag on the `cwctl ticket dct-action`
    # invocation built by the DRIVE_INSPECT action. Pulled from the
    # `ds.coreweave.com/physical-topology.zone` label (NOT `.region`) —
    # cwctl's ticket region is the granular zone string (e.g. `RNO2A`,
    # not `RNO2`). Empty when the label is absent.
    region: str = ""

    @property
    def search_identifiers(self) -> tuple[str, ...]:
        """Distinct identifiers that may appear in an HO ticket summary.

        Order is for stable rendering only — the JQL OR-joins them.
        Empty strings are dropped so we don't generate vacuous matches.
        """
        seen: list[str] = []
        for value in (self.bmn, self.cw_node, self.serial):
            if value and value not in seen:
                seen.append(value)
        return tuple(seen)

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
    # Set by resolve_ho_tickets() when a matching HO ticket is found. When
    # populated, execution updates the ticket via the JIRA runner instead
    # of running `command`. Mutually exclusive with `command` in practice:
    # the resolver always nulls out `command` when it sets `jira_issue`.
    jira_issue: str | None = None


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


def _drive_inspect_command(cw_node: str, serial: str, region: str) -> str:
    """Render the cwctl ticket creation for an onsite drive inspect/install.

    Mirrors the operator's documented form:
      cwctl ticket dct-action device <SERIAL>
            -m "Drives not detected in chassis. Please inspect and/or
                install the missing drive(s) for this GH200 node
                (<CWNODE> | SN: <SERIAL>)"
            -r <REGION>
    """
    message = (
        "Drives not detected in chassis. Please inspect and/or install the "
        f"missing drive(s) for this GH200 node ({cw_node} | SN: {serial})"
    )
    return f'cwctl ticket dct-action device {serial} -m "{message}" -r {region}'


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

    # Drive-inspect comes after power-drain (which is the more generic
    # remediation; if a node has both, power-drain runs first and the drive
    # issue persists in the next pass) but before HO-ticket — onsite drive
    # work needs to be scheduled before any administrative escalation.
    if target.sku in DRIVE_INSPECT_ELIGIBLE_SKUS:
        matching = sorted(codes_present & DRIVE_NOT_DETECTED_CODES)
        if matching:
            return PlannedAction(
                bmn=target.bmn,
                cw_node=target.cw_node,
                sku=target.sku,
                kind=ActionKind.DRIVE_INSPECT,
                triggering_codes=tuple(matching),
                command=_drive_inspect_command(
                    cw_node=target.cw_node,
                    serial=target.serial,
                    region=target.region,
                ),
                notes=(
                    f"Detected {', '.join(matching)} — will file a DO ticket "
                    "for onsite drive inspect/install unless one already exists."
                ),
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

    for kind in (
        ActionKind.POWER_DRAIN,
        ActionKind.HO_TICKET,
        ActionKind.XID_109_RETURN_TO_READY,
        ActionKind.DRIVE_INSPECT,
        ActionKind.IBP_RESEAT,
        ActionKind.NOOP,
    ):
        bucket = by_kind.get(kind, [])
        if not bucket:
            continue
        lines.append(f"\n[{kind.value}] {len(bucket)} node(s)")
        for action in bucket:
            # Colors: BMN names yellow, HO ticket keys cyan. CW-NODE / SKU
            # stay uncolored — they're context, not the operator's pivot.
            lines.append(f"  - {yellow(action.bmn)} (CW-NODE={action.cw_node}, SKU={action.sku})")
            if action.triggering_codes:
                lines.append(f"      codes: {', '.join(action.triggering_codes)}")
            if action.jira_issue:
                lines.append(f"      jira: append to {cyan(action.jira_issue)} description")
            elif action.command:
                lines.append(f"      command: {action.command}")
            lines.append(f"      note: {action.notes}")

    totals = ", ".join(f"{kind.value}={len(by_kind.get(kind, []))}" for kind in ActionKind)
    lines.append(f"\nTotals: {totals}")
    return "\n".join(lines)


# ── HO ticket resolution (between classify and execute) ──────────────────────

# Type alias: (identifiers, default_statuses) -> first-matching issue key or None.
JIRASearchFn = Callable[[tuple[str, ...]], str | None]


def resolve_ho_tickets(
    actions: Iterable[PlannedAction],
    search_fn: JIRASearchFn,
    targets_by_bmn: dict[str, BMNTarget],
) -> list[PlannedAction]:
    """For each HO_TICKET action, look up the matching JIRA ticket.

    If `search_fn` returns an issue key, replace the action's command with
    None and set `jira_issue` so execution updates the ticket instead of
    running return-to-triage. If nothing matches, the action is unchanged
    and the cwctl fallback runs as before.

    `search_fn` is injected for testability. The CLI binds it to a
    JIRAClient.search wrapper that builds JQL via build_search_jql.
    `targets_by_bmn` maps action.bmn → BMNTarget so the resolver can use
    each target's full identifier set (bmn, cw_node, serial).
    """
    resolved: list[PlannedAction] = []
    for action in actions:
        if action.kind is not ActionKind.HO_TICKET:
            resolved.append(action)
            continue

        target = targets_by_bmn.get(action.bmn)
        identifiers = target.search_identifiers if target else (action.bmn,)
        issue_key = search_fn(identifiers)
        if not issue_key:
            resolved.append(action)
            continue

        resolved.append(
            PlannedAction(
                bmn=action.bmn,
                cw_node=action.cw_node,
                sku=action.sku,
                kind=action.kind,
                triggering_codes=action.triggering_codes,
                command=None,  # JIRA update replaces the cwctl fallback
                notes=(
                    f"Found open HO ticket {issue_key} matching "
                    f"{', '.join(identifiers)} — would append a status block "
                    "to its Description."
                ),
                jira_issue=issue_key,
            )
        )
    return resolved


def render_jira_block(action: PlannedAction, timestamp: str) -> str:
    """Build the wiki-format text appended to an HO ticket's Description.

    `timestamp` is passed in (not generated here) so callers control the
    format and tests stay deterministic. Use ISO-8601 UTC at the call site.
    """
    codes = ", ".join(action.triggering_codes) if action.triggering_codes else "(none)"
    return (
        f"---- frops update {timestamp} ----\n"
        f"BMN:       {action.bmn}\n"
        f"CW-NODE:   {action.cw_node}\n"
        f"SKU:       {action.sku}\n"
        f"Detected:  {codes}\n"
        f"Note:      {action.notes}"
    )


# ── Phase B: execution ────────────────────────────────────────────────────────


def actionable_actions(actions: Iterable[PlannedAction]) -> list[PlannedAction]:
    """Return only actions that have something to run — shell or JIRA.

    Used by the CLI to decide whether to prompt at all (no actionable items =
    nothing to confirm) and what to hand to `execute_plan`. NOOP actions
    (both `command` and `jira_issue` are None) are dropped.
    """
    return [a for a in actions if a.command is not None or a.jira_issue is not None]


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of running one PlannedAction's command."""

    action: PlannedAction
    rc: int

    @property
    def succeeded(self) -> bool:
        return self.rc == 0


@dataclass(frozen=True)
class ExecutionSummary:
    """All ExecutionResults from one execute_plan() pass."""

    results: tuple[ExecutionResult, ...]

    @property
    def succeeded(self) -> tuple[ExecutionResult, ...]:
        return tuple(r for r in self.results if r.succeeded)

    @property
    def failed(self) -> tuple[ExecutionResult, ...]:
        return tuple(r for r in self.results if not r.succeeded)

    @property
    def worst_rc(self) -> int:
        # max() over an empty iterable would raise; default 0 == "all good".
        return max((r.rc for r in self.results), default=0)


def execute_plan(
    actions: Iterable[PlannedAction],
    runner: Callable[[str], int],
    jira_runner: Callable[[PlannedAction], int] | None = None,
) -> ExecutionSummary:
    """Dispatch each action to the right runner; collect per-action results.

    - `jira_issue` set → `jira_runner(action)` is called. `jira_runner` is
      required whenever any action has `jira_issue` set; passing None when
      one is needed raises ValueError so the caller fails fast instead of
      silently skipping the JIRA update.
    - `command` set, `jira_issue` None → `runner(command)`.
    - Both None → skipped silently (NOOP).

    Partial failure does NOT abort. Worst rc is returned via the summary.
    """
    results: list[ExecutionResult] = []
    for action in actions:
        if action.jira_issue is not None:
            if jira_runner is None:
                raise ValueError(
                    f"execute_plan: action {action.bmn} has jira_issue "
                    f"{action.jira_issue!r} but no jira_runner was provided"
                )
            rc = jira_runner(action)
        elif action.command is not None:
            rc = runner(action.command)
        else:
            continue
        results.append(ExecutionResult(action=action, rc=rc))
    return ExecutionSummary(results=tuple(results))


def render_execution_summary(summary: ExecutionSummary) -> str:
    """Human-readable post-run report — counts + failed-BMN list."""
    if not summary.results:
        return "=== Execution summary ===\n\n(no actionable items were executed)"

    lines: list[str] = [
        "=== Execution summary ===",
        f"\nRan {len(summary.results)} action(s): "
        f"{len(summary.succeeded)} succeeded, {len(summary.failed)} failed.",
    ]
    if summary.failed:
        lines.append("\nFailures:")
        for r in summary.failed:
            lines.append(f"  - {yellow(r.action.bmn)} [{r.action.kind.value}] exit {r.rc}")
            if r.action.jira_issue:
                lines.append(f"      jira:    append to {cyan(r.action.jira_issue)}")
            elif r.action.command:
                lines.append(f"      command: {r.action.command}")
    return "\n".join(lines)
