"""Access-check pass for NOOP-no-codes BMNs.

`view sku --action` classifies BMNs into power-drain / HO-ticket / NOOP
buckets. NOOP-no-codes nodes are the ambiguous ones: they're sitting in
a non-production state but AWX shows no CW errors to act on. Often the
reason is simpler than a software issue — the BMC is unreachable. This
module runs `jumpipmitool -c "chassis power status" <BMN>` (alongside
`bmns -o wide <BMN>` for the canonical TS / WORKFLOW / STATE display)
and surfaces the results as a diagnostic block after the planned-actions
plan.

Pure-functional + dependency-injected: a `capture` callable takes the
shell command and returns `(stdout, rc)`, mirroring `frops.commands.
capture_command`. Tests drive it without touching ipmitool / kubectl.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from frops.action import ActionKind, BMNTarget, PlannedAction

# (stdout, rc) — matches frops.commands.capture_command's contract.
# Variadic so callers can pass `timeout=<seconds>` without breaking tests
# that ship a 1-arg fake capture.
CaptureFn = Callable[..., tuple[str, int]]

DEFAULT_MAX_WORKERS: int = 8
# Per-call wall clock for the BMC reachability probe. Unreachable BMCs
# typically wedge on the IPMI handshake; without a ceiling a single
# offline node can stall the whole pass for minutes.
IPMI_TIMEOUT_SECONDS: float = 20.0
IPMI_TEMPLATE: str = 'jumpipmitool -c "chassis power status" {bmn}'
BMNS_WIDE_TEMPLATE: str = "bmns -o wide {bmn}"


@dataclass(frozen=True)
class AccessReport:
    """Per-BMN result from one access-check pass."""

    bmn: str
    cw_node: str
    workflow: str
    workflow_step: str
    state: str
    ts: str  # `bmns -o wide` formatted duration, e.g. "10m" / "2h" / "3d"
    reachable: bool
    detail: str  # first useful line of ipmitool output (or error excerpt)


def access_check_targets(
    actions: Iterable[PlannedAction],
    targets_by_bmn: dict[str, BMNTarget],
) -> list[BMNTarget]:
    """BMNs in the NOOP bucket with zero detected CW codes.

    Targets with non-actionable codes (e.g. CW0203 on a non-eligible SKU)
    are excluded — the message there already explains why the action was
    skipped, so a BMC reachability test wouldn't add new info.
    """
    out: list[BMNTarget] = []
    for action in actions:
        if action.kind is not ActionKind.NOOP:
            continue
        if action.triggering_codes:  # has codes, just not actionable
            continue
        target = targets_by_bmn.get(action.bmn)
        if target is not None:
            out.append(target)
    return out


def check_access(target: BMNTarget, capture: CaptureFn) -> AccessReport:
    """Run jumpipmitool + bmns-wide for one BMN, return a combined report.

    The IPMI call is capped at IPMI_TIMEOUT_SECONDS. When the timeout fires,
    capture_command returns rc=124 (GNU `timeout` convention); we surface
    that as an explicit "timed out after Ns" detail so operators can tell
    a timeout apart from other failure modes.
    """
    ipmi_out, ipmi_rc = capture(
        IPMI_TEMPLATE.format(bmn=target.bmn),
        timeout=IPMI_TIMEOUT_SECONDS,
    )
    reachable = ipmi_rc == 0
    if ipmi_rc == 124:
        detail = f"timed out after {int(IPMI_TIMEOUT_SECONDS)}s"
    else:
        detail = _first_useful_line(ipmi_out) or (
            "(no ipmitool output)" if reachable else f"exit {ipmi_rc}"
        )

    bmns_out, _ = capture(BMNS_WIDE_TEMPLATE.format(bmn=target.bmn))
    ts = _extract_ts(bmns_out)

    return AccessReport(
        bmn=target.bmn,
        cw_node=target.cw_node,
        workflow=target.workflow or "(unknown)",
        workflow_step=target.workflow_step or "(unknown)",
        state=target.state or "(unknown)",
        ts=ts,
        reachable=reachable,
        detail=detail,
    )


def check_all(
    targets: list[BMNTarget],
    capture: CaptureFn,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[AccessReport]:
    """Run check_access across targets in parallel; results sorted by BMN."""
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        reports = list(ex.map(lambda t: check_access(t, capture), targets))
    return sorted(reports, key=lambda r: r.bmn)


def render_access_summary(reports: list[AccessReport]) -> str:
    """Human-readable table of access-check results."""
    if not reports:
        return ""

    reachable = sum(1 for r in reports if r.reachable)
    unreachable = len(reports) - reachable

    lines: list[str] = [
        "=== Access check (NOOP + missing CW-NODE) ===",
        f"\nChecked {len(reports)} node(s): {reachable} reachable, {unreachable} unreachable.",
        "",
    ]

    # Right-pad each column to the widest value in that column so the table
    # stays aligned without depending on terminal-rendering libraries.
    # `cw_node` is rendered as "(none)" when empty (CW-NODE-less BMNs) so
    # the row is unambiguously not a normal NOOP-clean node.
    cols: list[tuple[str, list[str]]] = [
        ("BMN", [r.bmn for r in reports]),
        ("CW-NODE", [r.cw_node or "(none)" for r in reports]),
        ("WORKFLOW", [r.workflow for r in reports]),
        ("WORKFLOW-STEP", [r.workflow_step for r in reports]),
        ("STATE", [r.state for r in reports]),
        ("TS", [r.ts for r in reports]),
        ("REACH", ["yes" if r.reachable else "no" for r in reports]),
    ]
    widths = [max(len(h), max(len(v) for v in vs)) for h, vs in cols]
    header = "  ".join(h.ljust(w) for (h, _), w in zip(cols, widths, strict=True))
    lines.append(header)
    lines.append("  ".join("-" * w for w in widths))
    for i, r in enumerate(reports):
        row_values = [vs[i] for _, vs in cols]
        lines.append("  ".join(v.ljust(w) for v, w in zip(row_values, widths, strict=True)))
        # Detail line indented under the row for failures / interesting output.
        if not r.reachable or r.detail not in ("Chassis Power is on", "Chassis Power is off"):
            lines.append(f"    ↳ {r.detail}")

    return "\n".join(lines)


def render_missing_cwnode_summary(targets: list[BMNTarget]) -> str:
    """Dedicated section for BMNs that have no CW-NODE.

    These appear above the planned-actions block (they can't be classified
    by AWX, so they're not in any action group), and the same BMNs also
    show up in the access-check table when `[n]` is selected so the
    operator gets reachability info too.
    """
    if not targets:
        return ""

    lines: list[str] = [
        f"=== BMNs missing CW-NODE ({len(targets)}) ===",
        "",
        "No `status.reportedNodeInfo.nodeName` is set on these BMNs — AWX",
        "can't be queried for them, so they're not classified into an action",
        "group. Reachability is included in the access check when [n] is",
        "selected.",
        "",
    ]

    cols: list[tuple[str, list[str]]] = [
        ("BMN", [t.bmn for t in targets]),
        ("WORKFLOW", [t.workflow or "(unknown)" for t in targets]),
        ("WORKFLOW-STEP", [t.workflow_step or "(unknown)" for t in targets]),
        ("STATE", [t.state or "(unknown)" for t in targets]),
    ]
    widths = [max(len(h), max(len(v) for v in vs)) for h, vs in cols]
    lines.append("  ".join(h.ljust(w) for (h, _), w in zip(cols, widths, strict=True)))
    lines.append("  ".join("-" * w for w in widths))
    for i in range(len(targets)):
        row = [vs[i] for _, vs in cols]
        lines.append("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    return "\n".join(lines)


# ── Internals ────────────────────────────────────────────────────────────────


def _first_useful_line(output: str) -> str:
    """First non-blank line of output, trimmed and length-capped."""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _extract_ts(bmns_wide_output: str) -> str:
    """Pull the TS column value out of a single-BMN `bmns -o wide` output.

    Splits the data row on 2+ whitespace (kubectl's column separator) and
    zips with the header tokens. This is robust against values wider than
    their header's display width (a long STATE value would otherwise shift
    columns and corrupt position-based slicing).

    Returns "?" if the output is malformed or TS isn't present, so the
    access summary still renders.
    """
    lines = [line for line in bmns_wide_output.splitlines() if line.strip()]
    if len(lines) < 2:
        return "?"

    headers = re.findall(r"\S+", lines[0])
    if "TS" not in headers:
        return "?"
    values = re.split(r"\s{2,}", lines[1].strip())
    ts_index = headers.index("TS")
    if ts_index >= len(values):
        return "?"
    return values[ts_index] or "?"
