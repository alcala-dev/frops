"""Minimal Atlassian JIRA Cloud client used by `view sku --action`.

Scope: search for HO tickets by BMN/serial/CW-NODE and append a block to
the matched ticket's Description. Nothing else. We deliberately avoid
adding `requests`/`httpx` as a runtime dep — the surface is small enough
that stdlib `urllib` is fine, and keeping `frops` zero-runtime-dep makes
distribution trivial.

Auth: HTTP Basic with email + API token, read from the environment
(`JIRA_EMAIL` + `JIRA_TOKEN`). Generate a token at
https://id.atlassian.com/manage-profile/security/api-tokens.

Network behavior: requests time out after 15 s. Non-2xx responses raise
`JIRAError` with the response body for debugging. Search returns at most
50 issues — HO ticket lookups should be far below that.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL: str = "https://coreweave.atlassian.net"
DEFAULT_PROJECT: str = "HO"
DEFAULT_STATUSES: tuple[str, ...] = ("Awaiting Support",)
SEARCH_TIMEOUT_SECONDS: int = 15
SEARCH_MAX_RESULTS: int = 50


class JIRAError(RuntimeError):
    """Raised when JIRA returns a non-success status or auth is missing."""


@dataclass(frozen=True)
class JIRAIssue:
    """Trimmed view of a JIRA issue — only the fields the planner uses."""

    key: str
    summary: str
    status: str


def build_search_jql(
    project: str,
    identifiers: Iterable[str],
    statuses: Iterable[str] = DEFAULT_STATUSES,
) -> str:
    """Render a JQL string that matches a project's open tickets by identifier.

    The text-match operator `~` is case-insensitive and handles whitespace,
    so we pass each identifier as a quoted phrase. Empty identifiers are
    dropped to avoid degenerate `summary ~ ""` clauses.
    """
    cleaned_ids = [i for i in identifiers if i]
    if not cleaned_ids:
        raise ValueError("build_search_jql requires at least one identifier")

    id_clauses = " OR ".join(f'summary ~ "{_escape_jql_text(i)}"' for i in cleaned_ids)
    status_clauses = " OR ".join(f'status = "{_escape_jql_text(s)}"' for s in statuses if s)

    # Always quote the project key — some valid JIRA project keys (e.g. `DO`,
    # `OR`, `AND`, `IN`, `NOT`) collide with reserved JQL words and JIRA
    # rejects the unquoted form with HTTP 400. Quoting is safe for all keys.
    parts = [f'project = "{_escape_jql_text(project)}"']
    if status_clauses:
        parts.append(f"({status_clauses})")
    parts.append(f"({id_clauses})")
    return " AND ".join(parts)


def _escape_jql_text(value: str) -> str:
    # JQL text values escape backslash and double-quote with a backslash.
    return value.replace("\\", "\\\\").replace('"', '\\"')


class JIRAClient:
    """Auth + transport for the small subset of JIRA APIs we use."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        email: str | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email if email is not None else os.environ.get("JIRA_EMAIL", "")
        self._token = token if token is not None else os.environ.get("JIRA_TOKEN", "")
        if not self._email or not self._token:
            raise JIRAError(
                "JIRA credentials missing — set JIRA_EMAIL and JIRA_TOKEN in the "
                "environment (generate a token at "
                "https://id.atlassian.com/manage-profile/security/api-tokens)."
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def search(self, jql: str) -> list[JIRAIssue]:
        """Run JQL via /rest/api/3/search/jql. Returns at most SEARCH_MAX_RESULTS.

        Atlassian retired the legacy `/rest/api/3/search` endpoint (HTTP 410
        as of 2025); we hit the JQL-specific replacement instead. Payload
        shape is the same for our use case (jql, maxResults, fields). The
        new endpoint paginates via `nextPageToken` rather than start/total,
        but we only ever consume `issues[0]` so we never need a second page.
        See: https://developer.atlassian.com/changelog/#CHANGE-2046
        """
        payload = {
            "jql": jql,
            "maxResults": SEARCH_MAX_RESULTS,
            "fields": ["summary", "status"],
        }
        body = self._request("POST", "/rest/api/3/search/jql", payload)
        issues_raw = body.get("issues") or []
        return [_issue_from_json(raw) for raw in issues_raw]

    def append_to_description(self, issue_key: str, block: str) -> None:
        """Append `block` (Atlassian wiki markup) to an issue's Description.

        Reads the current Description as plain-text wiki via `expand=renderedFields`
        is unnecessary — we ask for the raw `description.text` field (`*all`) and
        re-PUT the concatenated value. If the current description is empty or
        missing, the block becomes the entire description.
        """
        # GET current description as ADF (Atlassian Document Format) wrapper or
        # null. JIRA Cloud may return ADF; the simplest correct approach for
        # arbitrary appends is to use the wiki-markup endpoint via the
        # `properties` or set the description as a text node. We use the
        # `update` API with the "set" verb and a plain string — JIRA Cloud
        # accepts string and renders it as ADF text.
        current = self.fetch_description(issue_key)
        new_description = f"{current}\n\n{block}" if current else block
        self._request(
            "PUT",
            f"/rest/api/3/issue/{issue_key}",
            {"fields": {"description": _wrap_as_adf(new_description)}},
        )

    def fetch_description(self, issue_key: str) -> str:
        """Plain-text view of an issue's Description (ADF flattened to text).

        Public because the XID-109 classifier scans descriptions for the
        XID 109 mention. Returns "" when the issue has no description.
        """
        body = self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}?fields=description",
            payload=None,
        )
        fields = body.get("fields") or {}
        return _adf_to_text(fields.get("description"))

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Authenticated JSON request. Raises JIRAError on non-2xx."""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None

        creds = base64.b64encode(f"{self._email}:{self._token}".encode()).decode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Basic {creds}",
                "User-Agent": "frops/jira",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=SEARCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500] if exc.fp else ""
            raise JIRAError(
                f"JIRA {method} {path} failed: HTTP {exc.code} {exc.reason}\n{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise JIRAError(f"JIRA {method} {path} could not reach host: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError as exc:
            raise JIRAError(f"JIRA returned non-JSON for {path}: {exc}") from exc


def _issue_from_json(raw: dict[str, Any]) -> JIRAIssue:
    fields = raw.get("fields") or {}
    status = (fields.get("status") or {}).get("name") or ""
    return JIRAIssue(
        key=raw.get("key", ""),
        summary=fields.get("summary") or "",
        status=status,
    )


def _wrap_as_adf(text: str) -> dict[str, Any]:
    """Wrap a plain-text/wiki string as a minimal ADF document.

    JIRA Cloud's REST v3 expects Description as ADF JSON. For non-formatted
    appends we wrap each line as a paragraph + text node. Blank lines become
    empty paragraphs (renders as visible spacing in the ticket).
    """
    blocks: list[dict[str, Any]] = []
    for line in text.splitlines() or [""]:
        if line:
            blocks.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
            )
        else:
            blocks.append({"type": "paragraph"})
    return {"type": "doc", "version": 1, "content": blocks}


def _adf_to_text(adf: Any) -> str:
    """Flatten an ADF document back to plain text for our append concat.

    We only care about preserving the bytes between our appended blocks —
    formatting fidelity isn't required. Recurse over `content`, emit each
    text node's `text`, and split paragraphs with blank lines.
    """
    if not adf or not isinstance(adf, dict):
        return ""
    lines: list[str] = []

    def _walk(node: dict[str, Any]) -> str:
        if node.get("type") == "text":
            return str(node.get("text") or "")
        children = node.get("content") or []
        return "".join(_walk(c) for c in children if isinstance(c, dict))

    for block in adf.get("content") or []:
        if isinstance(block, dict):
            lines.append(_walk(block))
    return "\n\n".join(lines).rstrip()
