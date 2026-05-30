"""Sanity checks for the static catalog."""

from __future__ import annotations

from frops.catalog import (
    ANALYZE_COMMANDS,
    FAIL_COMMANDS,
    FAIL_TYPES,
    OWNERSHIP_LABEL_TEMPLATE,
    SKU_EXCLUDED_STATES,
    SKU_VIEW_TEMPLATE,
)


def test_every_fail_type_has_a_command() -> None:
    for ft in FAIL_TYPES:
        assert ft in FAIL_COMMANDS, f"missing command for fail type: {ft}"


def test_fail_commands_end_with_single_quote() -> None:
    # build_command relies on splicing into a trailing-quoted label selector.
    for ft, cmd in FAIL_COMMANDS.items():
        assert cmd.endswith("'"), f"{ft} command must end with a quoted selector"


def test_ownership_template_renders() -> None:
    rendered = OWNERSHIP_LABEL_TEMPLATE.format(user="jdoe")
    assert rendered == "ownership.coreweave.com/owner=jdoe"


def test_analyze_templates_render_with_name() -> None:
    for target, steps in ANALYZE_COMMANDS.items():
        for label, template in steps:
            rendered = template.format(name="ss929610x4724071")
            assert "ss929610x4724071" in rendered, f"{target}/{label} dropped the name placeholder"


def test_sku_template_ends_with_single_quote() -> None:
    # _splice_user_filter assumes a trailing-quoted selector.
    assert SKU_VIEW_TEMPLATE.endswith("'")


def test_sku_template_renders_excluded_states_in_order() -> None:
    rendered = SKU_VIEW_TEMPLATE.format(sku="GPU-GH200-01")
    expected_states = ",".join(SKU_EXCLUDED_STATES)
    assert f"notin ({expected_states})" in rendered
    assert "ds.coreweave.com/sku.cw-sku=GPU-GH200-01" in rendered
