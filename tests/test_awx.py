"""Tests for the awxstat text parser."""

from __future__ import annotations

import textwrap

from frops.awx import AWXReport, CWError, parse_awxstat

# A real-shaped sample from awxstat with one CW code.
FAILED_MGMT_SAMPLE = textwrap.dedent(
    """\
    ====================================================================================

    Node:       S900770X4200980
    Limit:      10.168.212.129
    Job ID:     320078
    Job Name:   fwmanager
    Job URL:    https://awx.rno2.int.coreweave.com/#/jobs/playbook/320078/output
    Job Status: failed

    FAILED_TASKS=[
      {
        task: "Verify BIOS attributes"
        message: "Wrong parameters are provided: {'UEFIApplicationBootOption_1': 'Disabled'}"
      },
      {
        task: "BIOS attributes not present on system"
        message: "CW0211: Attributes not present on system. https://coreweave.atlassian.net/wiki/x/kwJ0F
    Debug data: foo"
      }
    ]

    cw_error_codes={
      CW0211: Attributes not present on system. https://coreweave.atlassian.net/wiki/x/kwJ0F
    }
    """
)

SUCCESSFUL_BMC_SAMPLE = textwrap.dedent(
    """\
    ====================================================================================

    Node:       S900770X4200980
    Limit:      10.168.20.129
    Job ID:     320072
    Job Name:   dpu-update
    Job URL:    https://awx.rno2.int.coreweave.com/#/jobs/playbook/320072/output
    Job Status: successful

    FAILED_TASKS=[]

    cw_error_codes={}
    """
)


def test_parse_awxstat_headers() -> None:
    report = parse_awxstat(FAILED_MGMT_SAMPLE)
    assert report.node == "S900770X4200980"
    assert report.limit == "10.168.212.129"
    assert report.job_id == "320078"
    assert report.job_name == "fwmanager"
    assert report.job_url is not None and report.job_url.endswith("/output")
    assert report.job_status == "failed"


def test_parse_awxstat_finds_cw_error() -> None:
    report = parse_awxstat(FAILED_MGMT_SAMPLE)
    assert report.cw_error_codes == (
        CWError(
            code="CW0211",
            description="Attributes not present on system. https://coreweave.atlassian.net/wiki/x/kwJ0F",
        ),
    )


def test_parse_awxstat_empty_cw_block_is_empty_tuple() -> None:
    report = parse_awxstat(SUCCESSFUL_BMC_SAMPLE)
    assert report.cw_error_codes == ()
    assert report.job_status == "successful"


def test_parse_awxstat_handles_multiple_cw_codes() -> None:
    text = textwrap.dedent(
        """\
        Node:       NX
        Job Status: failed

        cw_error_codes={
          CW0211: bios attributes missing
          CW0102: something else https://wiki/x/abc
        }
        """
    )
    report = parse_awxstat(text)
    codes = {err.code for err in report.cw_error_codes}
    assert codes == {"CW0211", "CW0102"}


def test_parse_awxstat_missing_fields_become_none() -> None:
    # Minimal text with no headers and no CW block.
    report = parse_awxstat("(nothing here)\n")
    assert isinstance(report, AWXReport)
    assert report.node is None
    assert report.job_id is None
    assert report.cw_error_codes == ()


def test_parse_awxstat_preserves_raw_text() -> None:
    report = parse_awxstat(FAILED_MGMT_SAMPLE)
    assert report.raw == FAILED_MGMT_SAMPLE
