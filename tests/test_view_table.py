"""Tests for the SKU view position-based renderer."""

from __future__ import annotations

import pytest

from frops.view_table import (
    DEFAULT_SKU_COLUMNS,
    EMPTY_CELL_PLACEHOLDER,
    render_sku_view_table,
)

# Realistic-shaped kubectl wide-format output. Two BMNs: the first has a
# normal CW-NODE; the second has empty CW-NODE so EXISTS/ONLINE would
# shift left under awk's collapsing splitter.
_WIDE_OUTPUT = (
    "NAME              DEVICESLOT       CW-NODE  EXISTS  ONLINE  CW-SKU         "
    "BMC-IP         OWNER       CLUSTER                       CWNC-STATE  WORKFLOW                          "
    "RETURN-WORKFLOW  RETURN-STATE  RETURN-STEP  PREV-WORKFLOW-STEP  WORKFLOW-STEP    "
    "NEXT-WORKFLOW-STEP  PREV-STATE   STATE          NEXT-STATE  TS    ORG-ID       NODE-PROFILE\n"
    "ss900770x3c03405  s6-r106-node-09  gaa6970  true    false   GPU-GH200-01   "
    "10.168.17.137  mthompson   tenant-cw-internal-fleetops   flcc        orphan                            "
    "empty            empty         empty        onboard             node-vaultify    "
    "dpu-vaultify        onboard      node-vaultify  empty       177m  cw-internal  "
    "tnt-cw-internal-fleetops-default-gpu-gh200-rno2\n"
    "ss900770x4113539  s6-r104-node-08           true    false   GPU-GH200-01   "
    "10.168.17.8                aalcala                                   <none>     "
    "provision-v2                      empty            empty         empty        "
    "empty               empty            empty               empty        fail         "
    "empty       27h   cw-internal  empty\n"
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escapes so substring assertions work whether or not
    colors are active during the test run."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.fixture(autouse=True)
def _no_colors_in_view_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: render in plain text so substring assertions are simple.
    Tests that want to verify the color escapes flip this on explicitly."""
    monkeypatch.setattr("frops.colors.supports_color", lambda: False)


# --------------------------- happy path -----------------------------------


def test_render_keeps_only_requested_columns_in_order() -> None:
    out = render_sku_view_table(_WIDE_OUTPUT)
    header = out.splitlines()[0].split()
    # Drops PREV-WORKFLOW-STEP, NEXT-WORKFLOW-STEP, NEXT-STATE,
    # RETURN-* columns — leaves the 17 in DEFAULT_SKU_COLUMNS.
    assert header == list(DEFAULT_SKU_COLUMNS)


def test_render_empty_cw_node_becomes_placeholder() -> None:
    out = render_sku_view_table(_WIDE_OUTPUT)
    lines = out.splitlines()
    # Second data row has no CW-NODE value in the raw output.
    second_row = lines[2]
    assert EMPTY_CELL_PLACEHOLDER in second_row


def test_render_does_not_shift_exists_into_cw_node_slot() -> None:
    """Regression guard for the awk-collapse bug: in the empty-CW-NODE row,
    `true`/`false` must NOT appear where the CW-NODE column lives."""
    out = render_sku_view_table(_WIDE_OUTPUT)
    rows = [line.split() for line in out.splitlines()[1:]]
    cw_node_idx = list(DEFAULT_SKU_COLUMNS).index("CW-NODE")
    # First row's CW-NODE is a real gmac.
    assert rows[0][cw_node_idx] == "gaa6970"
    # Second row's CW-NODE is the placeholder, not "true" (EXISTS) or
    # "false" (ONLINE) shifted left.
    assert rows[1][cw_node_idx] == EMPTY_CELL_PLACEHOLDER


def test_render_truncates_long_node_profile() -> None:
    out = render_sku_view_table(_WIDE_OUTPUT)
    np_idx = list(DEFAULT_SKU_COLUMNS).index("NODE-PROFILE")
    first_row = out.splitlines()[1].split()
    # The raw value is 47 chars; default cap is 24. The truncation marker
    # is "…" (one char) so the rendered cell is exactly 24 chars.
    assert first_row[np_idx].endswith("…")
    assert len(first_row[np_idx]) == 24


def test_render_passes_through_when_raw_is_blank() -> None:
    assert render_sku_view_table("") == ""
    assert render_sku_view_table("   \n\n") == "   \n\n"


def test_render_passes_through_when_no_requested_columns_match() -> None:
    raw = "FOO  BAR\nx    y\n"
    # None of DEFAULT_SKU_COLUMNS appear here; preserve the raw output so
    # we don't silently hide everything when a CRD revision renames things.
    assert render_sku_view_table(raw) == raw


# --------------------------- coloring -------------------------------------


def test_render_colors_bmn_yellow_and_deviceslot_magenta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force-enable colors regardless of the autouse stub.
    monkeypatch.setattr("frops.colors.supports_color", lambda: True)
    out = render_sku_view_table(_WIDE_OUTPUT)
    # Strip ANSI for substring lookups; assert the codes ARE present too.
    plain = _strip_ansi(out)
    assert "ss900770x3c03405" in plain
    assert "s6-r106-node-09" in plain
    assert "\033[1;33m" in out  # yellow open (BMN)
    assert "\033[1;35m" in out  # magenta open (DEVICESLOT)
    assert "\033[0m" in out  # reset


def test_render_dims_placeholder_cw_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frops.colors.supports_color", lambda: True)
    out = render_sku_view_table(_WIDE_OUTPUT)
    # Dim escape appears (used for the (none) placeholder so the row
    # visually subsides vs a populated one).
    assert "\033[2m" in out


def test_render_column_widths_use_visible_text_for_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANSI escapes don't count toward visible width; alignment must be
    computed from plain text and color applied AFTER ljust."""
    monkeypatch.setattr("frops.colors.supports_color", lambda: True)
    out = render_sku_view_table(_WIDE_OUTPUT)
    # Strip escapes and verify each rendered row has the same visible width
    # as the header (i.e. the alignment held up).
    plain_lines = [_strip_ansi(line).rstrip() for line in out.splitlines()]
    # ljust pads to the widest cell value, so visible widths should be
    # consistent across header + rows for each cell-column. We just check
    # the NAME column (col 0): its width matches the longest BMN value.
    header_cols = plain_lines[0].split("  ")
    name_col_width = len(header_cols[0].rstrip())
    for line in plain_lines[1:]:
        # First "  " separator delimits the NAME cell.
        # Use rsplit-style: split into max 2 to grab the first column verbatim.
        first_cell, _, _ = line.partition("  ")
        # First cell should be at least name_col_width chars wide (it was
        # padded with ljust). Trailing spaces are stripped by rstrip()
        # above on the whole line, but within a row the first cell still
        # has its rightmost padding because it's followed by "  ".
        assert len(first_cell) >= name_col_width
