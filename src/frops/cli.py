"""FROps CLI — argparse setup and subcommand handlers."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from frops import __version__
from frops.access import (
    access_check_targets,
    check_all,
    render_access_summary,
    render_missing_cwnode_summary,
)
from frops.action import (
    ActionKind,
    BMNTarget,
    PlannedAction,
    actionable_actions,
    classify,
    execute_plan,
    render_execution_summary,
    render_jira_block,
    render_plan,
    resolve_ho_tickets,
)
from frops.awx import AWXReport, parse_awxstat
from frops.catalog import (
    ANALYZE_COMMANDS,
    FAIL_COMMANDS,
    FAIL_TYPES,
    OWNERSHIP_LABEL_TEMPLATE,
    SKU_VIEW_TEMPLATE,
    SKU_VIEW_TEMPLATE_JSON,
)
from frops.commands import capture_command, run_command
from frops.jira import (
    DEFAULT_PROJECT as JIRA_PROJECT,
)
from frops.jira import (
    DEFAULT_STATUSES as JIRA_OPEN_STATUSES,
)
from frops.jira import (
    JIRAClient,
    JIRAError,
    build_search_jql,
)
from frops.xid109 import (
    XID109Candidate,
    collect_xid109_candidates,
    fetch_phase_reason,
    parse_cwnc_states,
    render_waiting_summary,
    return_to_ready_command,
    split_by_actionable,
)

VIEW_TYPES: tuple[str, ...] = (*FAIL_TYPES, "sku")


def _splice_user_filter(base: str, user_filter: str | None) -> str:
    """Splice an ownership label into a base command's trailing quoted selector.

    The base command ends with `…'`; the owner filter is inserted just before
    the closing quote so the resulting selector stays a single, valid kubectl
    argument.
    """
    if not user_filter:
        return base
    ownership_label = OWNERSHIP_LABEL_TEMPLATE.format(user=user_filter)
    return base[:-1] + f",{ownership_label}'"


def build_command(fail_type: str, user_filter: str | None) -> str:
    """Return the kubectl command for a fail type, optionally filtered by owner."""
    return _splice_user_filter(FAIL_COMMANDS[fail_type], user_filter)


def build_sku_command(sku: str, user_filter: str | None) -> str:
    """Return the kubectl command for a SKU view, optionally filtered by owner."""
    return _splice_user_filter(SKU_VIEW_TEMPLATE.format(sku=sku), user_filter)


def build_sku_command_json(sku: str, user_filter: str | None) -> str:
    """Return the JSON-output variant of the SKU view command.

    Used by `--action` to get structured BMN data (CW-NODE, labels) without
    affecting the human-facing colored output of the wide view.
    """
    return _splice_user_filter(SKU_VIEW_TEMPLATE_JSON.format(sku=sku), user_filter)


def format_section(label: str, cmd: str, output: str, rc: int) -> str:
    """Format a single analyze section with a labeled block header.

    Only used on the dry-run path; live runs stream the command's own output
    (preserving colors emitted by kubectl/kubecolor and friends).
    """
    status = f"  [exit {rc}]" if rc != 0 else ""
    body = output.rstrip() or "(no output)"
    return "\n".join([f"### {label} ###{status}", f"# {cmd}", "", body, ""])


def handle_view(args: argparse.Namespace) -> int:
    action_flag = getattr(args, "action", False)
    yes_flag = getattr(args, "yes", False)

    if yes_flag and not action_flag:
        print(
            "error: --yes requires --action (nothing to confirm without a plan)",
            file=sys.stderr,
        )
        return 2

    if args.fail_type == "sku":
        if not args.sku_value:
            print(
                "error: 'view sku' requires a SKU argument (e.g. GPU-GH200-01)",
                file=sys.stderr,
            )
            return 2
        cmd = build_sku_command(args.sku_value, args.user_filter)
        subject = f"sku={args.sku_value}"
    else:
        if args.sku_value:
            print(
                f"error: 'view {args.fail_type}' takes no extra positional argument",
                file=sys.stderr,
            )
            return 2
        if action_flag:
            print(
                "error: --action is only supported with 'view sku'",
                file=sys.stderr,
            )
            return 2
        cmd = build_command(args.fail_type, args.user_filter)
        subject = args.fail_type

    owner_label = f" (owner: {args.user_filter})" if args.user_filter else ""
    print(f"Viewing: {subject}{owner_label}")
    print(f"Command: {cmd}\n")

    if args.dry_run:
        if action_flag:
            note = "(--action would also fetch JSON BMN data + per-BMN awxstat output"
            note += "; --yes would then execute the rendered cwctl commands)" if yes_flag else ")"
            print(note)
        return 0

    view_rc = run_command(cmd)
    if not action_flag:
        return view_rc

    plan_rc = _run_sku_action_plan(args.sku_value, args.user_filter, auto_yes=yes_flag)
    return max(view_rc, plan_rc)


def _run_sku_action_plan(sku: str, user_filter: str | None, *, auto_yes: bool = False) -> int:
    """Fetch BMN JSON + awxstat output, classify, print a plan, optionally execute.

    Phase A behavior (auto_yes=False, no prompt response, or all NOOPs) prints
    the plan and returns 0. Phase B execution runs each actionable command via
    `run_command` and returns the worst exit code among them.

    Returns a non-zero exit on hard failures (kubectl JSON fetch fails, JSON
    parse fails) or on partial execution failure. Per-BMN awxstat failures are
    logged and skipped — the BMN is excluded from the plan rather than
    aborting the whole pass.
    """
    json_cmd = build_sku_command_json(sku, user_filter)
    raw, rc = capture_command(json_cmd)
    if rc != 0:
        print(
            f"error: failed to fetch BMN JSON (exit {rc}):\n{raw}",
            file=sys.stderr,
        )
        return rc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: failed to parse kubectl JSON output: {exc}", file=sys.stderr)
        return 1

    items = data.get("items", [])
    if not items:
        print("\n=== Planned actions ===\n\n(no BMNs returned by the SKU query)")
        return 0

    targets, missing_cwnode = _build_targets(items, sku)

    if missing_cwnode:
        print()
        print(render_missing_cwnode_summary(missing_cwnode))

    actions = [classify(target) for target in targets]

    # Resolve HO-ticket actions against JIRA before rendering the plan so
    # the user sees "would update HO-12345" rather than the cwctl fallback.
    # Soft-fail on any JIRA issue (auth missing, network down): the actions
    # stay on the cwctl fallback path and we surface the reason once.
    jira_client = _try_make_jira_client()
    targets_by_bmn = {t.bmn: t for t in targets}
    if jira_client is not None and any(a.kind is ActionKind.HO_TICKET for a in actions):
        actions = _resolve_with_jira(actions, jira_client, targets_by_bmn)

    # XID-109 detour: for triage BMNs whose HO ticket mentions XID 109, run
    # the return-to-ready pipeline. Power-drain wins (per spec) — only NOOP
    # and HO-ticket actions are eligible to be reclassified as XID-109.
    # JIRA is required; we soft-skip when JIRA is unavailable.
    xid109_actionable, xid109_waiting = _maybe_run_xid109_pipeline(actions, targets, jira_client)
    if xid109_actionable:
        actions = _apply_xid109_overrides(actions, xid109_actionable)
    if xid109_waiting:
        print()
        print(render_waiting_summary(xid109_waiting))

    print()
    print(render_plan(actions))

    actionable = actionable_actions(actions)
    # Combined pool for the access pass: NOOP-no-codes from the plan AND any
    # CW-NODE-less BMNs. The reachability probe is the same shell call for
    # both, and the operator wants both groups checked under one prompt.
    access_pool = access_check_targets(actions, targets_by_bmn) + list(missing_cwnode)

    if not actionable and not access_pool:
        print("\nNothing to do.")
        return 0

    # Per-group selection: which ActionKinds run via execute_plan, and
    # whether the access-check pass fires. --yes selects everything;
    # otherwise we prompt with letter shortcuts
    # ([a]ll/[p]/[h]/[x]/[n]/Enter).
    if auto_yes:
        selected_kinds: set[ActionKind] = {a.kind for a in actionable}
        run_access = bool(access_pool)
    else:
        selected_kinds, run_access = _prompt_group_selection(actionable, access_pool)
        if not selected_kinds and not run_access:
            print("Aborted by user. Nothing was executed.")
            return 0

    plan_rc = 0
    to_execute = [a for a in actionable if a.kind in selected_kinds]
    if to_execute:
        print()  # blank line before cwctl streams its own output
        summary = execute_plan(
            to_execute,
            run_command,
            jira_runner=_build_jira_runner(jira_client) if jira_client is not None else None,
        )
        print()
        print(render_execution_summary(summary))
        plan_rc = summary.worst_rc

    if run_access and access_pool:
        print()
        reports = check_all(access_pool, capture_command)
        print(render_access_summary(reports))

    return plan_rc


def _try_make_jira_client() -> JIRAClient | None:
    """Construct JIRAClient or print a one-line note and return None.

    HO-ticket lookup is best-effort: missing creds shouldn't break power-drain
    actions or the cwctl fallback. We print a single visible reason so the
    user knows why JIRA matching was skipped.
    """
    try:
        return JIRAClient()
    except JIRAError as exc:
        print(f"\nnote: JIRA lookup disabled — {exc}", file=sys.stderr)
        return None


def _maybe_run_xid109_pipeline(
    actions: list[PlannedAction],
    targets: list[BMNTarget],
    jira_client: JIRAClient | None,
) -> tuple[list[XID109Candidate], list[XID109Candidate]]:
    """Return (actionable, waiting) XID-109 candidates, or ([], []) if skipped.

    The pipeline only runs when (a) JIRA is available and (b) at least one
    target is currently in a non-power-drain action class. Power-drain
    actions are never eligible — they take precedence per the spec.
    Failures along the way (bmns wide error, JIRA error, jsonpath error)
    surface a single stderr note and the pipeline reports nothing rather
    than aborting the larger plan.
    """
    if jira_client is None:
        return [], []
    eligible_targets = [
        t
        for t in targets
        if not any(a.bmn == t.bmn and a.kind is ActionKind.POWER_DRAIN for a in actions)
    ]
    if not eligible_targets:
        return [], []

    cwnc_states = _fetch_cwnc_states([t.bmn for t in eligible_targets])
    if not cwnc_states:
        return [], []

    def _search(identifiers: tuple[str, ...]) -> str | None:
        try:
            jql = build_search_jql(JIRA_PROJECT, identifiers, JIRA_OPEN_STATUSES)
            issues = jira_client.search(jql)
        except (JIRAError, ValueError) as exc:
            print(
                f"warning: XID-109 JIRA search failed for {identifiers}: {exc}",
                file=sys.stderr,
            )
            return None
        return issues[0].key if issues else None

    def _fetch_desc(issue_key: str) -> str:
        try:
            return jira_client.fetch_description(issue_key)
        except JIRAError as exc:
            print(
                f"warning: XID-109 description fetch failed for {issue_key}: {exc}",
                file=sys.stderr,
            )
            return ""

    def _fetch_phase(bmn: str) -> str:
        return fetch_phase_reason(bmn, capture_command)

    candidates = collect_xid109_candidates(
        eligible_targets,
        cwnc_states,
        jira_search=_search,
        fetch_description=_fetch_desc,
        fetch_phase=_fetch_phase,
    )
    return split_by_actionable(candidates)


def _fetch_cwnc_states(bmn_names: list[str]) -> dict[str, str]:
    """One `bmns -o wide` call for many BMNs → {name: CWNC-STATE}.

    bmns CLI accepts space-separated names. We pass all at once to avoid
    N round-trips. On non-zero exit we surface the stderr note and return
    {} so the XID-109 pipeline degrades quietly.
    """
    if not bmn_names:
        return {}
    cmd = "bmns -o wide " + " ".join(bmn_names)
    out, rc = capture_command(cmd)
    if rc != 0:
        print(
            f"warning: XID-109 bmns -o wide failed (exit {rc}):\n{out.strip()[:300]}",
            file=sys.stderr,
        )
        return {}
    return parse_cwnc_states(out)


def _apply_xid109_overrides(
    actions: list[PlannedAction],
    xid109_actionable: list[XID109Candidate],
) -> list[PlannedAction]:
    """Replace each matching action with an XID_109_RETURN_TO_READY entry.

    Power-drain rows are left alone (the pipeline already excluded them).
    HO-ticket and NOOP entries get the new kind + return-to-ready command
    + a note that points at the matching JIRA ticket.
    """
    actionable_by_bmn = {c.bmn: c for c in xid109_actionable}
    rewritten: list[PlannedAction] = []
    for action in actions:
        cand = actionable_by_bmn.get(action.bmn)
        if cand is None or action.kind is ActionKind.POWER_DRAIN:
            rewritten.append(action)
            continue
        rewritten.append(
            PlannedAction(
                bmn=cand.bmn,
                cw_node=cand.cw_node,
                sku=cand.sku,
                kind=ActionKind.XID_109_RETURN_TO_READY,
                triggering_codes=action.triggering_codes,
                command=return_to_ready_command(cand.bmn),
                notes=(
                    f"CWNC-STATE=triage; HO ticket {cand.jira_issue} mentions XID 109; "
                    f"PhaseState reason={cand.phase_reason} → both controllers in triage"
                ),
                jira_issue=None,  # the XID-109 cwctl is the action; no JIRA write
            )
        )
    return rewritten


def _resolve_with_jira(
    actions: list[PlannedAction],
    client: JIRAClient,
    targets_by_bmn: dict[str, BMNTarget],
) -> list[PlannedAction]:
    """Build the search closure and run the resolver. Soft-fail on JIRAError."""

    def _search(identifiers: tuple[str, ...]) -> str | None:
        try:
            jql = build_search_jql(JIRA_PROJECT, identifiers, JIRA_OPEN_STATUSES)
            issues = client.search(jql)
        except (JIRAError, ValueError) as exc:
            # Per-BMN failure: log once and fall back. Don't poison the whole
            # batch — power-drain actions in the same plan should still run.
            print(
                f"warning: JIRA search failed for {identifiers}: {exc}",
                file=sys.stderr,
            )
            return None
        # First match wins. JIRA returns issues in default order (recently
        # updated); for HO ticket dedup that's the right pick.
        return issues[0].key if issues else None

    return resolve_ho_tickets(actions, _search, targets_by_bmn)


def _build_jira_runner(client: JIRAClient) -> Callable[[PlannedAction], int]:
    """Return a runner that appends a status block to action.jira_issue."""

    def _run(action: PlannedAction) -> int:
        assert action.jira_issue is not None  # guarded by execute_plan dispatch
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = render_jira_block(action, timestamp)
        try:
            client.append_to_description(action.jira_issue, block)
        except JIRAError as exc:
            print(
                f"error: failed to update {action.jira_issue} for {action.bmn}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"updated {action.jira_issue} for {action.bmn}")
        return 0

    return _run


def _prompt_group_selection(
    actionable: list[PlannedAction],
    access_targets: list[BMNTarget],
) -> tuple[set[ActionKind], bool]:
    """Letter-shortcut prompt: which groups should execute?

    Returns `(selected_kinds, run_access_check)`. Empty input, EOF, or
    Ctrl-C means abort — both return values are empty/False.

    Letter map (only the groups present in the plan are offered):
        a       → everything available
        p       → power-drain actions
        h       → ho-ticket actions
        n       → NOOP access check
        p,h,n   → comma-separated combinations

    Anything else (including empty/EOF) aborts. `n` (lowercase) is distinct
    from a missing/empty answer, so users can pick noop-only without it
    being interpreted as "no".
    """
    # Each option = (letter, kind-or-None, display-label, count). The letter
    # appears in brackets inside the label so the prompt reads like
    # `[p]ower-drain (3)` instead of `[p]power-drain (3)`.
    options: list[tuple[str, ActionKind | None, str, int]] = []
    counts_by_kind: dict[ActionKind, int] = {}
    for a in actionable:
        counts_by_kind[a.kind] = counts_by_kind.get(a.kind, 0) + 1
    if ActionKind.POWER_DRAIN in counts_by_kind:
        options.append(
            ("p", ActionKind.POWER_DRAIN, "[p]ower-drain", counts_by_kind[ActionKind.POWER_DRAIN])
        )
    if ActionKind.HO_TICKET in counts_by_kind:
        options.append(
            ("h", ActionKind.HO_TICKET, "[h]o-ticket", counts_by_kind[ActionKind.HO_TICKET])
        )
    if ActionKind.XID_109_RETURN_TO_READY in counts_by_kind:
        options.append(
            (
                "x",
                ActionKind.XID_109_RETURN_TO_READY,
                "[x]id-109-return",
                counts_by_kind[ActionKind.XID_109_RETURN_TO_READY],
            )
        )
    if access_targets:
        # ActionKind.NOOP would be misleading — NOOP actions have no command.
        # The `n` letter triggers the diagnostic access check pass instead.
        options.append(("n", None, "[n]oop-access", len(access_targets)))

    if not options:
        return set(), False

    parts = ["[a]ll"] + [f"{label} ({count})" for _, _, label, count in options]
    parts.append("Enter to abort")
    prompt = "\nRun? " + "  /  ".join(parts) + ": "

    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()  # newline so the next print doesn't sit on the prompt line
        return set(), False

    if not ans:
        return set(), False

    if ans == "a":
        kinds = {kind for _, kind, _, _ in options if kind is not None}
        return kinds, any(letter == "n" for letter, _, _, _ in options)

    selected_kinds: set[ActionKind] = set()
    run_access = False
    letter_to_kind = {letter: kind for letter, kind, _, _ in options}
    for piece in (p.strip() for p in ans.split(",")):
        if piece not in letter_to_kind:
            continue  # silently ignore unknown letters
        kind = letter_to_kind[piece]
        if kind is None:
            run_access = True
        else:
            selected_kinds.add(kind)
    return selected_kinds, run_access


def _build_targets(
    items: list[dict[str, Any]], default_sku: str
) -> tuple[list[BMNTarget], list[BMNTarget]]:
    """Build BMNTargets from raw kubectl JSON `items`.

    Returns `(targets, missing_cwnode)`. Both lists contain BMNTargets;
    items end up in `missing_cwnode` when `status.reportedNodeInfo.nodeName`
    is empty — those can't be classified by AWX (no node to query), so we
    skip awxstat and leave `awx_reports=()`. They still get full workflow /
    state / TS / SKU labels so the diagnostic surface (dedicated section
    + access check) has everything it needs.
    """
    targets: list[BMNTarget] = []
    missing_cwnode: list[BMNTarget] = []

    for item in items:
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        bmn_name = metadata.get("name") or ""
        reported_node = (status.get("reportedNodeInfo") or {}).get("nodeName") or ""

        if not bmn_name:
            continue

        labels = metadata.get("labels") or {}
        sku = labels.get("ds.coreweave.com/sku.cw-sku", default_sku)
        serial = labels.get("ds.coreweave.com/status.asset.serial", "")
        workflow = labels.get("flcc.coreweave.com/workflow", "")
        workflow_step = labels.get("flcc.coreweave.com/workflow-step", "")
        state = labels.get("flcc.coreweave.com/state", "")

        if not reported_node:
            missing_cwnode.append(
                BMNTarget(
                    bmn=bmn_name,
                    cw_node="",
                    sku=sku,
                    awx_reports=(),
                    serial=serial,
                    workflow=workflow,
                    workflow_step=workflow_step,
                    state=state,
                )
            )
            continue

        reports = _collect_awx_reports(bmn_name)
        targets.append(
            BMNTarget(
                bmn=bmn_name,
                cw_node=reported_node,
                sku=sku,
                awx_reports=tuple(reports),
                serial=serial,
                workflow=workflow,
                workflow_step=workflow_step,
                state=state,
            )
        )

    return targets, missing_cwnode


def _collect_awx_reports(bmn: str) -> list[AWXReport]:
    """Run awxstat -l mgmt|bmc against a BMN and parse the output.

    Failures (awxstat unavailable, non-zero exit) are logged to stderr and
    the limit type is skipped. Phase A continues with whatever it got.
    """
    reports: list[AWXReport] = []
    for limit_type in ("mgmt", "bmc"):
        text, rc = capture_command(f"awxstat -l {limit_type} {bmn}")
        if rc != 0:
            print(
                f"warning: awxstat -l {limit_type} {bmn} failed (exit {rc}); skipping this limit",
                file=sys.stderr,
            )
            continue
        reports.append(parse_awxstat(text))
    return reports


def handle_analyze(args: argparse.Namespace) -> int:
    steps = ANALYZE_COMMANDS[args.target]
    print(f"\n### Analyzing {args.target}: {args.name} ###\n")

    worst_rc = 0
    for label, template in steps:
        cmd = template.format(name=args.name)
        if args.dry_run:
            print(format_section(label, cmd, "(dry-run, not executed)", 0))
            continue
        # Stream the command's output directly so terminal colors emitted by
        # kubectl/kubecolor/yq are preserved end-to-end. Capturing would force
        # the tools into non-TTY mode and strip ANSI codes.
        print(f"### {label} ###")
        print(f"# {cmd}")
        rc = run_command(cmd)
        if rc != 0:
            print(f"[exit {rc}]")
        print()
        worst_rc = max(worst_rc, rc)
    return worst_rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frops",
        description="FROps workflow helper CLI tool.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print the commands that would run without executing them.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    view_parser = subparsers.add_parser("view", help="View types of failures.")
    view_parser.add_argument(
        "fail_type",
        choices=VIEW_TYPES,
        metavar="TYPE",
        help=(
            f"View type. Failure types: {', '.join(FAIL_TYPES)}. "
            "Use 'sku' for SKU-based filtering (requires a SKU value)."
        ),
    )
    view_parser.add_argument(
        "sku_value",
        nargs="?",
        default=None,
        metavar="SKU",
        help="SKU value, required when TYPE is 'sku' (e.g. GPU-GH200-01).",
    )
    view_parser.add_argument(
        "-u",
        metavar="USER",
        dest="user_filter",
        default=None,
        help=(
            "Filter results by ownership label. "
            "Pass 'unassigned' or a specific username, e.g. -u jdoe"
        ),
    )
    view_parser.add_argument(
        "--action",
        action="store_true",
        help=(
            "After displaying results, inspect AWX jobs per BMN, "
            "classify by CW codes, and print an action plan."
        ),
    )
    view_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=(
            "With --action: skip the confirmation prompt and execute the "
            "planned cwctl commands. Without --action this flag is rejected."
        ),
    )
    view_parser.set_defaults(func=handle_view)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a specific resource.")
    analyze_parser.add_argument(
        "target",
        choices=list(ANALYZE_COMMANDS.keys()),
        metavar="TARGET",
        help=f"Resource type to analyze. One of: {', '.join(ANALYZE_COMMANDS.keys())}",
    )
    analyze_parser.add_argument(
        "name",
        metavar="NAME",
        help="Name of the resource to analyze, e.g. ss929610x4724071",
    )
    analyze_parser.set_defaults(func=handle_analyze)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
