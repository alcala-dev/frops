"""Position-aware renderer for `kubectl get bmns -o wide` output.

Replaces the awk + column -t shell pipeline. Awk's default field splitter
collapses runs of whitespace into a single delimiter, which silently
shifts every column left of an empty cell — operators noticed when an
empty CW-NODE row showed `EXISTS`'s `false` value where `g123abc` belonged.

This renderer instead parses the header line to find each column's start
position and slices each data row at those positions, so empty cells
become empty strings (which we then substitute with `(none)` for CW-NODE
so the row isn't ambiguous). Long CLUSTER / NODE-PROFILE values are
truncated with `…` so the trimmed table fits on typical terminal widths.

Colors are applied at render time via `frops.colors`: BMN names yellow,
DEVICESLOT magenta. ANSI escapes don't count toward visible width, so
`.ljust(width)` is applied to the plain string before wrapping in color
sequences — alignment stays correct.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from frops.colors import dim, magenta, yellow

# Columns from the 23-wide kubectl output to keep, in display order.
DEFAULT_SKU_COLUMNS: tuple[str, ...] = (
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
    "WORKFLOW-STEP",
    "PREV-STATE",
    "STATE",
    "TS",
    "ORG-ID",
    "NODE-PROFILE",
)

# Replacement for cells where kubectl emits literal whitespace (e.g.
# empty CW-NODE on a BMN that hasn't joined a cluster yet). `(none)` is
# explicit, scans cleanly in the table, and matches the existing
# convention in `render_access_summary` for the same situation.
EMPTY_CELL_PLACEHOLDER: str = "(none)"

# Hard caps on columns operators occasionally need but that dominate the
# row width. Full values are still available via `bmns -o wide <BMN>` for
# deep dives. Tune these by editing this dict; nothing else changes.
TRUNCATE_LIMITS: dict[str, int] = {
    "CLUSTER": 28,
    "NODE-PROFILE": 24,
}

# Per-column color hooks. Each callable takes the (already-padded) plain
# string and returns the same string wrapped in ANSI escapes. Headers not
# listed here render uncolored.
COLUMN_COLORS: dict[str, Callable[[str], str]] = {
    "NAME": yellow,
    "DEVICESLOT": magenta,
}


def render_sku_view_table(
    raw: str,
    columns: tuple[str, ...] = DEFAULT_SKU_COLUMNS,
) -> str:
    """Return the kubectl wide output trimmed to `columns` and colorized.

    `raw` is the verbatim stdout of `kubectl get bmns -o wide -l '...'`.
    Header positions in the first non-blank line define column boundaries
    for every data row — so empty cells stay empty (not collapsed into the
    neighbor) and value-overflow stays in its own column.

    Returns the raw output unchanged when:
      - it's empty / blank (nothing to render), or
      - none of `columns` appear in the header (avoid hiding everything
        if a CRD revision renames a column).
    """
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return raw

    header_line = lines[0]
    matches = list(re.finditer(r"\S+", header_line))
    if not matches:
        return raw

    headers = [m.group() for m in matches]
    starts = [m.start() for m in matches]
    # Each column ends where the next one begins; the last runs to EOL.
    ends: list[int | None] = [*starts[1:], None]

    keep_idx = [headers.index(name) for name in columns if name in headers]
    if not keep_idx:
        return raw

    kept_headers = [headers[i] for i in keep_idx]

    rows: list[list[str]] = []
    for line in lines[1:]:
        row = [_extract_cell(line, headers[i], starts[i], ends[i]) for i in keep_idx]
        rows.append(row)

    widths = [len(h) for h in kept_headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    out: list[str] = []
    out.append("  ".join(h.ljust(w) for h, w in zip(kept_headers, widths, strict=True)))
    for row in rows:
        out.append(_render_row(row, widths, kept_headers))
    return "\n".join(out)


def _extract_cell(line: str, header: str, start: int, end: int | None) -> str:
    """Slice one cell from a data row; apply per-column placeholders / truncation."""
    cell = line[start:end] if end is not None else line[start:]
    cell = cell.strip()
    if not cell and header == "CW-NODE":
        return EMPTY_CELL_PLACEHOLDER
    limit = TRUNCATE_LIMITS.get(header)
    if limit is not None and len(cell) > limit:
        # Reserve the last slot for the ellipsis so the column width stays
        # at exactly `limit` characters.
        return cell[: limit - 1] + "…"
    return cell


def _render_row(row: list[str], widths: list[int], headers: list[str]) -> str:
    """Render one data row: ljust-pad first, then wrap colored cells."""
    parts: list[str] = []
    for cell, width, header in zip(row, widths, headers, strict=True):
        padded = cell.ljust(width)
        if cell == EMPTY_CELL_PLACEHOLDER:
            # Dim the placeholder so a `(none)` row visually subsides
            # vs a normal CW-NODE.
            padded = dim(padded)
        else:
            colorize = COLUMN_COLORS.get(header)
            if colorize is not None:
                padded = colorize(padded)
        parts.append(padded)
    return "  ".join(parts)
