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

# States that a 'view sku' query excludes — i.e. we only want to see nodes
# that are *not* healthy/operating-as-intended for the requested SKU.
SKU_EXCLUDED_STATES: Final[tuple[str, ...]] = (
    "production",
    "ready",
    "rma",
    "broken",
    "dev",
    "debug",
)

_SKU_EXCLUDED_RENDERED = ",".join(SKU_EXCLUDED_STATES)
SKU_VIEW_TEMPLATE: Final[str] = (
    "kubectl get bmns -o wide -l "
    "'ds.coreweave.com/sku.cw-sku={sku},"
    f"flcc.coreweave.com/state notin ({_SKU_EXCLUDED_RENDERED})'"
)

# Columns the SKU view should display from `kubectl get bmns -o wide`.
# The CRD's full wide format emits 23 columns; this trims off the columns
# that aren't useful for the at-a-glance triage view (the various
# *_WORKFLOW_STEP / NEXT-STATE columns), and reorders WORKFLOW-STEP to
# follow the RETURN-* group for readability.
SKU_VIEW_COLUMNS: Final[tuple[str, ...]] = (
    "NAME",
    "DEVICESLOT",
    "CW-NODE",
    "EXISTS",
    "ONLINE",
    "CW-SKU",
    "BMC-IP",
    "OWNER",
    "CLUSTER",
    "CWNC-STATE",
    "WORKFLOW",
    "RETURN-WORKFLOW",
    "RETURN-STATE",
    "RETURN-STEP",
    "WORKFLOW-STEP",
    "PREV-STATE",
    "STATE",
    "TS",
    "ORG-ID",
    "NODE-PROFILE",
)

# Same selector, JSON output. Used by '--action' to fetch structured BMN
# data (CW-NODE, labels) alongside the colored human display.
SKU_VIEW_TEMPLATE_JSON: Final[str] = (
    "kubectl get bmns -o json -l "
    "'ds.coreweave.com/sku.cw-sku={sku},"
    f"flcc.coreweave.com/state notin ({_SKU_EXCLUDED_RENDERED})'"
)

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
