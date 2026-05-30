"""Parser for `awxstat` text output.

`awxstat` prints a few labeled header lines followed by `FAILED_TASKS=[…]`
and `cw_error_codes={…}` blocks. Format isn't strict JSON/YAML, so this
module does regex-based best-effort parsing of the bits we care about.
For now Phase A only consumes the headers and the CW codes; the raw text
is preserved on the report so Phase B (HO ticket descriptions) can mine
failed-task messages later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CWError:
    """A CWXXXX error code reported by awxstat."""

    code: str
    description: str


@dataclass(frozen=True)
class AWXReport:
    """Structured view of one `awxstat -l <type> <BMN>` invocation."""

    node: str | None
    limit: str | None
    job_id: str | None
    job_name: str | None
    job_url: str | None
    job_status: str | None
    cw_error_codes: tuple[CWError, ...]
    raw: str


_HEADER_PATTERNS: dict[str, re.Pattern[str]] = {
    "node": re.compile(r"^Node:\s+(.+)$", re.MULTILINE),
    "limit": re.compile(r"^Limit:\s+(.+)$", re.MULTILINE),
    "job_id": re.compile(r"^Job ID:\s+(.+)$", re.MULTILINE),
    "job_name": re.compile(r"^Job Name:\s+(.+)$", re.MULTILINE),
    "job_url": re.compile(r"^Job URL:\s+(.+)$", re.MULTILINE),
    "job_status": re.compile(r"^Job Status:\s+(.+)$", re.MULTILINE),
}

_CW_BLOCK_RE = re.compile(r"cw_error_codes=\{(.*?)\}", re.DOTALL)
_CW_LINE_RE = re.compile(r"^\s*(CW\d+)\s*:\s*(.+?)\s*$", re.MULTILINE)


def parse_awxstat(text: str) -> AWXReport:
    """Parse the text emitted by `awxstat -l <type> <BMN>`.

    Missing sections yield `None` fields rather than raising, so callers can
    handle partial output (e.g. `awxstat` returning only a header when no
    AWX job exists yet).
    """
    headers: dict[str, str | None] = {}
    for key, pattern in _HEADER_PATTERNS.items():
        match = pattern.search(text)
        headers[key] = match.group(1).strip() if match else None

    cw_codes: list[CWError] = []
    block_match = _CW_BLOCK_RE.search(text)
    if block_match:
        body = block_match.group(1)
        for code_match in _CW_LINE_RE.finditer(body):
            cw_codes.append(
                CWError(code=code_match.group(1), description=code_match.group(2).strip())
            )

    return AWXReport(
        node=headers["node"],
        limit=headers["limit"],
        job_id=headers["job_id"],
        job_name=headers["job_name"],
        job_url=headers["job_url"],
        job_status=headers["job_status"],
        cw_error_codes=tuple(cw_codes),
        raw=text,
    )
