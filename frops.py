#!/usr/bin/env python3
"""FROps CLI — helper tool for viewing workflow failure types."""

import argparse
from modules.commands import run_command, capture_command


FAIL_TYPES = ["fails", "zapfails", "nodezapfails", "dpuzapfails", "testfails", "fielddiagfails"]
FAIL_COMMANDS = {
    "fails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail'",
    "zapfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state in (dpu-zap,node-zap)'",
    "nodezapfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=node-zap'",
    "dpuzapfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=dpu-zap'",
    "testfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=test'",
    "fielddiagfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=fielddiag'",
    "unassigned": "ownership.coreweave.com/owner={user}",
}

# Each analyze target maps to an ordered list of (label, command_template) pairs.
# Use {name} as a placeholder for the resource name argument.
ANALYZE_COMMANDS = {
    "bmn": [
        ("Overview",       "kubectl get bmns -o wide {name}"),
        ("Messages",       "kubectl get bmns {name} -o yaml | yq -r '.status.flcc.messages'"),
        ("AWX Mgmt",       "awxstat -l mgmt {name}"),
        ("AWX BMC",        "awxstat -l bmc {name}"),
    ],
}

def build_command(fail_type: str, user_filter: str | None) -> str:
    base = FAIL_COMMANDS[fail_type]

    if user_filter:
        ownership_label = FAIL_COMMANDS["unassigned"].format(user=user_filter)
        base = base[:-1] + f",{ownership_label}'"
    return base


def handle_view(args):
    fail_type = args.fail_type
    user_filter = args.user_filter
    if fail_type not in FAIL_COMMANDS:
        print(f"'{fail_type}' is not yet implemented.")
        return

    cmd = build_command(fail_type, user_filter)
    label = f" (owner: {user_filter})" if user_filter else ""
    print(f"Viewing: {fail_type}{label}")
    print(f"Command: {cmd}\n")
    run_command(cmd)

def _section(label: str, cmd: str, output: str, rc: int) -> str:
    """Format a single analyze section with a simple labeled block header."""
    status = f"  [exit {rc}]" if rc != 0 else ""
    lines = [
        f"### {label} ###{status}",
        f"# {cmd}",
        "",
        output.rstrip() or "(no output)",
        "",
    ]
    return "\n".join(lines)

def handle_analyze(args):
    target = args.target
    name   = args.name

    if target not in ANALYZE_COMMANDS:
        print(f"'{target}' is not yet implemented for analyze.")
        return

    steps = ANALYZE_COMMANDS[target]
    print(f"\n### Analyzing {target}: {name} ###\n")

    for label, template in steps:
        cmd = template.format(name=name)
        output, rc = capture_command(cmd)
        print(_section(label, cmd, output, rc))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FROps workflow helper CLI tool.")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # 'view' subcommand
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

    # 'analyze' subcommand
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


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()