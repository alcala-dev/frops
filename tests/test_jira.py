"""Tests for the JIRA module: JQL builder + transport + ADF helpers."""

from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from typing import Any
from unittest.mock import patch

import pytest

from frops.jira import (
    DEFAULT_PROJECT,
    DEFAULT_STATUSES,
    JIRAClient,
    JIRAError,
    JIRAIssue,
    _adf_to_text,
    _wrap_as_adf,
    build_search_jql,
)

# --------------------------- build_search_jql -------------------------------


def test_jql_single_identifier_emits_summary_and_status_clauses() -> None:
    jql = build_search_jql("HO", ["ss900770x4200980"])
    assert 'project = "HO"' in jql
    assert 'summary ~ "ss900770x4200980"' in jql
    assert 'status = "Awaiting Support"' in jql


def test_jql_multiple_identifiers_or_joins_summary_clauses() -> None:
    jql = build_search_jql("HO", ["ss900770x4200980", "g81b512", "S900770X4200980"])
    assert jql.count('summary ~ "') == 3
    assert " OR " in jql
    # All identifiers present:
    for ident in ("ss900770x4200980", "g81b512", "S900770X4200980"):
        assert f'"{ident}"' in jql


def test_jql_drops_empty_identifiers() -> None:
    jql = build_search_jql("HO", ["", "ss900770x4200980", ""])
    assert jql.count('summary ~ "') == 1
    assert "ss900770x4200980" in jql


def test_jql_raises_when_all_identifiers_empty() -> None:
    with pytest.raises(ValueError):
        build_search_jql("HO", ["", ""])


def test_jql_escapes_quotes_in_identifiers() -> None:
    jql = build_search_jql("HO", ['weird"id'])
    # JIRA's text operator expects backslash-escaped quotes.
    assert r'"weird\"id"' in jql


def test_jql_custom_statuses() -> None:
    jql = build_search_jql("HO", ["x"], statuses=("Awaiting Support", "Investigating"))
    assert 'status = "Awaiting Support"' in jql
    assert 'status = "Investigating"' in jql
    assert "OR" in jql


def test_jql_no_status_filter_when_statuses_empty() -> None:
    jql = build_search_jql("HO", ["x"], statuses=())
    assert "status" not in jql
    assert 'project = "HO"' in jql


def test_default_constants_have_expected_values() -> None:
    # Belt-and-suspenders: the CLI imports these by name.
    assert DEFAULT_PROJECT == "HO"
    assert DEFAULT_STATUSES == ("Awaiting Support",)


# --------------------------- ADF helpers ------------------------------------


def test_wrap_as_adf_round_trips_via_adf_to_text() -> None:
    original = "line one\nline two\n\nfinal"
    adf = _wrap_as_adf(original)
    assert adf["type"] == "doc"
    # Round-trip is approximate; the wrapper splits per line, the parser
    # joins on double-newline. Verify each substantive line survives.
    extracted = _adf_to_text(adf)
    for line in ("line one", "line two", "final"):
        assert line in extracted


def test_adf_to_text_handles_none_and_non_dict() -> None:
    assert _adf_to_text(None) == ""
    assert _adf_to_text("not a dict") == ""
    assert _adf_to_text(42) == ""


def test_adf_to_text_walks_nested_content() -> None:
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "second"}]},
        ],
    }
    assert _adf_to_text(adf) == "hello world\n\nsecond"


# --------------------------- JIRAClient construction ------------------------


def test_client_init_reads_env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "me@x.com")
    monkeypatch.setenv("JIRA_TOKEN", "tok-from-env")
    client = JIRAClient()
    assert client._email == "me@x.com"
    assert client._token == "tok-from-env"
    assert client.base_url == "https://coreweave.atlassian.net"


def test_client_init_raises_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    with pytest.raises(JIRAError, match="JIRA credentials missing"):
        JIRAClient()


def test_client_init_explicit_args_override_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "env@x.com")
    monkeypatch.setenv("JIRA_TOKEN", "env-tok")
    client = JIRAClient(email="arg@x.com", token="arg-tok", base_url="https://other/")
    assert client._email == "arg@x.com"
    assert client._token == "arg-tok"
    assert client.base_url == "https://other"  # trailing slash stripped


# --------------------------- JIRAClient transport ---------------------------


class _FakeHTTPResponse:
    """Minimal stand-in for urllib's HTTP response."""

    def __init__(self, payload: dict[str, Any] | bytes) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self._buf = BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self._buf.close()


def _client_with_creds(monkeypatch: pytest.MonkeyPatch) -> JIRAClient:
    monkeypatch.setenv("JIRA_EMAIL", "me@x.com")
    monkeypatch.setenv("JIRA_TOKEN", "tok")
    return JIRAClient()


def test_search_parses_issues_and_sends_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_creds(monkeypatch)
    sent: dict[str, Any] = {}

    def _fake_urlopen(req: Any, timeout: int = 0) -> _FakeHTTPResponse:
        sent["method"] = req.get_method()
        sent["url"] = req.full_url
        sent["auth"] = req.headers.get("Authorization")
        sent["body"] = json.loads(req.data) if req.data else None
        return _FakeHTTPResponse(
            {
                "issues": [
                    {
                        "key": "HO-12345",
                        "fields": {
                            "summary": "Server Node (ss900770x4200980) entered failed state (triage)",
                            "status": {"name": "Awaiting Support"},
                        },
                    }
                ]
            }
        )

    with patch("frops.jira.urllib.request.urlopen", _fake_urlopen):
        issues = client.search('project = "HO" AND summary ~ "ss900770x4200980"')

    assert issues == [
        JIRAIssue(
            key="HO-12345",
            summary="Server Node (ss900770x4200980) entered failed state (triage)",
            status="Awaiting Support",
        )
    ]
    assert sent["method"] == "POST"
    assert sent["url"].endswith("/rest/api/3/search/jql")
    assert sent["auth"].startswith("Basic ")
    assert sent["body"]["jql"].startswith('project = "HO"')
    assert sent["body"]["maxResults"] == 50


def test_search_returns_empty_list_when_no_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_creds(monkeypatch)
    with patch(
        "frops.jira.urllib.request.urlopen",
        lambda req, timeout=0: _FakeHTTPResponse({"issues": []}),
    ):
        assert client.search("anything") == []


def test_search_raises_jira_error_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    client = _client_with_creds(monkeypatch)

    def _raise(*_: object, **__: object) -> None:
        raise urllib.error.HTTPError(
            url="x",
            code=401,
            msg="Unauthorized",
            hdrs=Message(),
            fp=BytesIO(b'{"err":"bad token"}'),
        )

    with (
        patch("frops.jira.urllib.request.urlopen", _raise),
        pytest.raises(JIRAError, match="HTTP 401"),
    ):
        client.search("anything")


def test_append_to_description_fetches_then_puts_concatenated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_creds(monkeypatch)

    existing = "previous body\n\nsecond paragraph"
    calls: list[dict[str, Any]] = []

    def _fake_urlopen(req: Any, timeout: int = 0) -> _FakeHTTPResponse:
        body = json.loads(req.data) if req.data else None
        calls.append({"method": req.get_method(), "url": req.full_url, "body": body})
        if req.get_method() == "GET":
            return _FakeHTTPResponse({"fields": {"description": _wrap_as_adf(existing)}})
        # PUT — JIRA returns 204 / empty body in reality; we return {} so the
        # JSON parse short-circuits.
        return _FakeHTTPResponse(b"")

    with patch("frops.jira.urllib.request.urlopen", _fake_urlopen):
        client.append_to_description("HO-12345", "new block")

    assert [c["method"] for c in calls] == ["GET", "PUT"]
    # GET fetches just the description field
    assert "fields=description" in calls[0]["url"]
    # PUT body's description must be ADF wrapping the concatenation
    put_doc = calls[1]["body"]["fields"]["description"]
    flat = _adf_to_text(put_doc)
    assert "previous body" in flat
    assert "second paragraph" in flat
    assert "new block" in flat
    # New block must come AFTER the existing content
    assert flat.index("new block") > flat.index("second paragraph")


def test_append_to_description_handles_empty_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_creds(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _fake_urlopen(req: Any, timeout: int = 0) -> _FakeHTTPResponse:
        calls.append(
            {"method": req.get_method(), "body": json.loads(req.data) if req.data else None}
        )
        if req.get_method() == "GET":
            return _FakeHTTPResponse({"fields": {"description": None}})
        return _FakeHTTPResponse(b"")

    with patch("frops.jira.urllib.request.urlopen", _fake_urlopen):
        client.append_to_description("HO-12345", "only block")

    put_doc = calls[1]["body"]["fields"]["description"]
    assert _adf_to_text(put_doc).strip() == "only block"


# --------------------------- add_comment ------------------------------------


def test_add_comment_posts_adf_wrapped_body_to_comment_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_creds(monkeypatch)
    calls: list[dict[str, Any]] = []

    def _fake_urlopen(req: Any, timeout: int = 0) -> _FakeHTTPResponse:
        calls.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "body": json.loads(req.data) if req.data else None,
            }
        )
        # JIRA returns 201 + the new comment JSON, but we don't read it.
        return _FakeHTTPResponse(b"")

    with patch("frops.jira.urllib.request.urlopen", _fake_urlopen):
        client.add_comment("HO-42", "RMA to Vendor\n\nDevice: g123")

    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/rest/api/3/issue/HO-42/comment")
    # The body must be ADF-wrapped so line breaks survive the round trip.
    body_doc = calls[0]["body"]["body"]
    assert body_doc["type"] == "doc"
    flat = _adf_to_text(body_doc)
    assert "RMA to Vendor" in flat
    assert "Device: g123" in flat
