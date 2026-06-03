"""FROps CLI — argparse setup and subcommand handlers."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from frops import __version__
from frops.action import (
    BMNTarget,
    actionable_actions,
    classify,
    execute_plan,
    render_execution_summary,
    render_plan,
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

    targets, skipped = _build_targets(items, sku)

    if skipped:
        print(f"\n(skipped {len(skipped)} BMN(s) with empty CW-NODE: {', '.join(skipped)})")

    actions = [classify(target) for target in targets]
    print()
    print(render_plan(actions))

    actionable = actionable_actions(actions)
    if not actionable:
        print("\nNothing actionable to execute.")
        return 0

    if not auto_yes and not _prompt_yes_no(f"\nRun {len(actionable)} action(s) above?"):
        print("Aborted by user. No cwctl actions were executed.")
        return 0

    print()  # blank line before each cwctl command's own output starts streaming
    summary = execute_plan(actionable, run_command)
    print()
    print(render_execution_summary(summary))
    return summary.worst_rc


def _prompt_yes_no(prompt: str) -> bool:
    """Interactive y/N prompt. Returns False on empty/EOF/Ctrl-C/non-yes."""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()  # newline so the next print isn't on the prompt line
        return False
    return answer in ("y", "yes")


def _build_targets(
    items: list[dict[str, Any]], default_sku: str
) -> tuple[list[BMNTarget], list[str]]:
    """Build BMNTargets from raw kubectl JSON `items`.

    Returns `(targets, skipped_bmn_names)`. A BMN is "skipped" when it has
    no CW-NODE (i.e. `.status.reportedNodeInfo.nodeName` is empty) — those
    can't be actioned even if AWX shows failures, per the spec.
    """
    targets: list[BMNTarget] = []
    skipped: list[str] = []

    for item in items:
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        bmn_name = metadata.get("name") or ""
        reported_node = (status.get("reportedNodeInfo") or {}).get("nodeName") or ""

        if not bmn_name:
            continue
        if not reported_node:
            skipped.append(bmn_name)
            continue

        labels = metadata.get("labels") or {}
        sku = labels.get("ds.coreweave.com/sku.cw-sku", default_sku)

        reports = _collect_awx_reports(bmn_name)
        targets.append(
            BMNTarget(
                bmn=bmn_name,
                cw_node=reported_node,
                sku=sku,
                awx_reports=tuple(reports),
            )
        )

    return targets, skipped


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
