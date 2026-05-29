"""Static catalog of view selectors and analyze inspection steps.

Keeping these as data (rather than embedded in the CLI) makes it easy to
add new fail types or analyze targets without touching argparse plumbing,
and lets unit tests assert on the rendered commands directly.
"""

from __future__ import annotations

from typing import Final

FAIL_TYPES: Final[tuple[str, ...]] = (
    "fails",
    "zapfails",
    "nodezapfails",
    "dpuzapfails",
    "testfails",
    "fielddiagfails",
)

FAIL_COMMANDS: Final[dict[str, str]] = {
    "fails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail'",
    "zapfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state in (dpu-zap,node-zap)'",
    "nodezapfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=node-zap'",
    "dpuzapfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=dpu-zap'",
    "testfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=test'",
    "fielddiagfails": "kubectl get bmns -o wide -l 'flcc.coreweave.com/state=fail,flcc.coreweave.com/previous-state=fielddiag'",
}

OWNERSHIP_LABEL_TEMPLATE: Final[str] = "ownership.coreweave.com/owner={user}"

# Each analyze target maps to an ordered list of (label, command_template).
# Use {name} as the placeholder for the resource-name argument.
ANALYZE_COMMANDS: Final[dict[str, list[tuple[str, str]]]] = {
    "bmn": [
        ("Overview", "kubectl get bmns -o wide {name}"),
        ("Messages", "kubectl get bmns {name} -o yaml | yq -r '.status.flcc.messages'"),
        ("AWX Mgmt", "awxstat -l mgmt {name}"),
        ("AWX BMC", "awxstat -l bmc {name}"),
    ],
}
