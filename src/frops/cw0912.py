"""CW0912 progressive remediation workflow (GH200).

Stage 1 (this module): after a CW0912-triggered power-drain executes
successfully, schedule a node-zap rerun via `at` at +15 minutes so the
flcc workflow advances without operator intervention.

The CW0912 → power-drain classification itself lives in `frops.action`
(CW0912 is part of `POWER_DRAIN_CODES`). This module owns the
post-execution follow-up: rendering the rerun command, finding which
results need scheduling, and surfacing per-BMN scheduling outcomes.

Stages 2 (DCT tray-reseat after a persistent failure) and 3 (HO ticket
RMA escalation) are tracked separately and not implemented here yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from frops.action import ActionKind, BMNTarget, ExecutionResult, ExecutionSummary
from frops.colors import cyan, dim, yellow
from frops.schedule import schedule_at

CW0912: str = "CW0912"
# `at(1)` understands free-form time specs; "+15 minutes" matches the
# user's stated cadence and is portable across BSD/Linux variants.
NODE_ZAP_DELAY: str = "now + 15 minutes"

# Injectable for tests so we don't actually invoke at(1). Mirrors the
# (success, detail) contract from frops.schedule.schedule_at.
ScheduleFn = Callable[[str, str], tuple[bool, str]]


def node_zap_rerun_command(bmn: str, workflow: str) -> str:
    """Render the cwctl invocation that retries the node-zap step.

    Matches the operator-documented form:
      cwctl flcc node -w <WORKFLOW> -s node-zap <BMN>
            -m "rerunning node-zap after remote power-drain"
    """
    return (
        f"cwctl flcc node -w {workflow} -s node-zap {bmn} "
        '-m "rerunning node-zap after remote power-drain"'
    )


@dataclass(frozen=True)
class CW0912Rerun:
    """One BMN's scheduling outcome after a successful CW0912 power-drain.

    `scheduled` is True only when at(1) accepted the job. `detail`
    carries either the at(1) confirmation ("job 42 at <time>") or the
    failure reason ("`at` not found", "BMN missing workflow label").
    """

    bmn: str
    cw_node: str
    command: str
    scheduled: bool
    detail: str


def _is_cw0912_power_drain(result: ExecutionResult) -> bool:
    """True when the result is a successful POWER_DRAIN whose codes include CW0912.

    Other codes (CW0211, CW0102) also trigger POWER_DRAIN but do not
    need the node-zap rerun — only CW0912 has the documented two-step
    drain-then-rerun cadence.
    """
    return (
        result.succeeded
        and result.action.kind is ActionKind.POWER_DRAIN
        and CW0912 in result.action.triggering_codes
    )


def schedule_node_zap_reruns(
    summary: ExecutionSummary,
    targets_by_bmn: dict[str, BMNTarget],
    *,
    schedule: ScheduleFn = schedule_at,
    delay: str = NODE_ZAP_DELAY,
) -> list[CW0912Rerun]:
    """For every successful CW0912 power-drain in `summary`, queue a node-zap rerun.

    Returns one CW0912Rerun per affected BMN — both successes and
    failures — so the caller can surface a single per-BMN row. BMNs
    whose workflow label is missing get a `scheduled=False` record
    with a descriptive detail; we don't attempt the at call without a
    workflow.
    """
    reruns: list[CW0912Rerun] = []
    for result in summary.results:
        if not _is_cw0912_power_drain(result):
            continue
        target = targets_by_bmn.get(result.action.bmn)
        if target is None or not target.workflow:
            reruns.append(
                CW0912Rerun(
                    bmn=result.action.bmn,
                    cw_node=result.action.cw_node,
                    command="",
                    scheduled=False,
                    detail="BMN missing `flcc.coreweave.com/workflow` label — cannot render node-zap rerun",
                )
            )
            continue
        command = node_zap_rerun_command(target.bmn, target.workflow)
        ok, detail = schedule(command, delay)
        reruns.append(
            CW0912Rerun(
                bmn=result.action.bmn,
                cw_node=result.action.cw_node,
                command=command,
                scheduled=ok,
                detail=detail,
            )
        )
    return reruns


def render_node_zap_rerun_summary(reruns: list[CW0912Rerun]) -> str:
    """Render the post-execution table of scheduled node-zap reruns.

    Empty input → "" so the caller can `if not summary` skip. Successful
    rows show the at(1) confirmation; failed rows show the error so the
    operator can decide whether to run the command manually (the
    rendered command is printed too so it's copy-pasteable).
    """
    if not reruns:
        return ""

    lines: list[str] = [
        f"=== CW0912 node-zap reruns scheduled ({len(reruns)}) ===",
        "",
        "Each successful CW0912 power-drain queues a `cwctl flcc node -s",
        "node-zap` rerun via at(1) at +15 minutes. Keep your tsh/Doppler",
        "session alive — the at job runs as you, so cwctl auth must still",
        "be valid when the timer fires. Failed schedules show the rendered",
        "command so you can run it manually after the 15-minute window.",
        "",
    ]

    cols: list[tuple[str, list[str]]] = [
        ("BMN", [r.bmn for r in reruns]),
        ("CW-NODE", [r.cw_node or "(none)" for r in reruns]),
        ("STATUS", ["scheduled" if r.scheduled else "FAILED" for r in reruns]),
        ("DETAIL", [r.detail.splitlines()[0] if r.detail else "" for r in reruns]),
    ]
    widths = [max(len(h), max(len(v) for v in vs)) for h, vs in cols]
    lines.append("  ".join(h.ljust(w) for (h, _), w in zip(cols, widths, strict=True)))
    lines.append("  ".join("-" * w for w in widths))
    for i in range(len(reruns)):
        row = [vs[i] for _, vs in cols]
        cells = [v.ljust(w) for v, w in zip(row, widths, strict=True)]
        cells[0] = yellow(cells[0])  # BMN
        if reruns[i].scheduled:
            cells[2] = cyan(cells[2])  # STATUS=scheduled
        lines.append("  ".join(cells))

    # Show the rendered command for any failed scheduling so the
    # operator can run it manually (or copy into their own scheduler).
    failed = [r for r in reruns if not r.scheduled and r.command]
    if failed:
        lines.append("")
        lines.append(dim("Failed schedules — run after +15 minutes:"))
        for r in failed:
            lines.append(dim(f"  {r.bmn}: {r.command}"))

    return "\n".join(lines)
