"""FROps CLI — argparse setup and subcommand handlers."""

from __future__ import annotations

import argparse

from frops import __version__
from frops.catalog import (
    ANALYZE_COMMANDS,
    FAIL_COMMANDS,
    FAIL_TYPES,
    OWNERSHIP_LABEL_TEMPLATE,
)
from frops.commands import capture_command, run_command


def build_command(fail_type: str, user_filter: str | None) -> str:
    """Return the kubectl command for a fail type, optionally filtered by owner.

    The base command ends with a quoted label selector; the owner filter is
    spliced in just before the closing quote so the resulting selector stays
    a single, valid kubectl argument.
    """
    base = FAIL_COMMANDS[fail_type]
    if not user_filter:
        return base

    ownership_label = OWNERSHIP_LABEL_TEMPLATE.format(user=user_filter)
    return base[:-1] + f",{ownership_label}'"


def format_section(label: str, cmd: str, output: str, rc: int) -> str:
    """Format a single analyze section with a labeled block header."""
    status = f"  [exit {rc}]" if rc != 0 else ""
    body = output.rstrip() or "(no output)"
    return "\n".join([f"### {label} ###{status}", f"# {cmd}", "", body, ""])


def handle_view(args: argparse.Namespace) -> int:
    cmd = build_command(args.fail_type, args.user_filter)
    owner_label = f" (owner: {args.user_filter})" if args.user_filter else ""
    print(f"Viewing: {args.fail_type}{owner_label}")
    print(f"Command: {cmd}\n")

    if args.dry_run:
        return 0
    return run_command(cmd)


def handle_analyze(args: argparse.Namespace) -> int:
    steps = ANALYZE_COMMANDS[args.target]
    print(f"\n### Analyzing {args.target}: {args.name} ###\n")

    worst_rc = 0
    for label, template in steps:
        cmd = template.format(name=args.name)
        if args.dry_run:
            print(format_section(label, cmd, "(dry-run, not executed)", 0))
            continue
        output, rc = capture_command(cmd)
        print(format_section(label, cmd, output, rc))
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
        choices=FAIL_TYPES,
        metavar="TYPE",
        help=f"Failure type to view. One of: {', '.join(FAIL_TYPES)}",
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
