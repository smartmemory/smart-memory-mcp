"""STUDIO-DECISION-VIEW-1 U5: history and sites tools over the REST backend (the hosted connector's path), plus the
plain-words renderers. RECORDED responses (contract and ``report-u4-dv-service-common.md``), not a live service."""

from __future__ import annotations

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import decision_tools
from smartmemory_mcp.tools.common import history_text, sites_text, who_text


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools(monkeypatch, responder):
    seen = []
    backend = RemoteBackend.__new__(RemoteBackend)

    def request(method, path, **kwargs):
        seen.append((method, path, kwargs))
        return responder(method, path, kwargs)

    backend._request = request
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    return mcp.tools, seen


REPLACED = {
    "decision_id": "d1",
    "visible": True,
    "at": "2026-10-01T09:00:00+00:00",
    "by": "human:alice",
    "reason": "capacity review Q4",
    "how": "direct",
}
D0 = {
    "decision_id": "d0",
    "content": "Each site may enroll at most 15 participants.",
    "status": "superseded",
    "recorded_by": "human:bob",
    "recorded_for": None,
    "created_at": "2026-09-01T09:00:00+00:00",
    "decided_by": "protocol committee",
    "replaced": REPLACED,
    "replaces": [],
}
HIDDEN = {**REPLACED, "decision_id": None, "visible": False, "by": None, "reason": None}


def test_decision_get_says_the_one_call_answer_in_words(monkeypatch):
    tools, seen = _tools(monkeypatch, lambda *a: D0)
    text = tools["decision_get"](decision_id="d0")
    assert "Recorded by: human:bob" in text
    assert "Replaced by: d1 on 2026-10-01T09:00:00+00:00 by human:alice, reason: capacity review Q4." in text
    assert seen[0][:2] == ("GET", "/memory/decisions/d0")


def test_a_hidden_replacement_reads_as_the_frozen_wording_with_no_id_text_or_link():
    text = who_text({**D0, "replaced": HIDDEN})
    assert "replaced by a rule you cannot see" in text
    assert "None" not in text and "d1" not in text
    assert "Replaces: a rule you cannot see." in who_text({"replaces": [HIDDEN]})
    sites = {
        "at": "2026-10-15T00:00:00+00:00",
        "all_sites": False,
        "rows": [{"scope": {"site": "01"}, "in_force": {"decision_id": None, "visible": False, "from": "2026-10-01"}, "next": None}],
    }
    assert "a rule you cannot see (from 2026-10-01)" in sites_text("d1", sites)
    entry = {"at": "t", "action": "replaced_by", "related_decision_id": None, "related_visible": False}
    assert "(other decision: a rule you cannot see)" in history_text("d0", {"entries": [entry]})


def test_a_read_without_the_new_fields_prints_nothing_extra():
    assert who_text({"decision_id": "d9", "content": "x"}) == ""
    assert who_text({"created_at": None}) == "Recorded on: date unknown (before history was kept)"


def test_review_approval_and_agent_for_person_wording():
    held = {**REPLACED, "how": "review", "proposed_by": "agent:agent-x", "approved_by": "api_key:key-ci"}
    text = who_text({"recorded_by": "agent:agent-x", "recorded_for": "human:bob", "replaced": held})
    assert "Recorded by: agent:agent-x for human:bob" in text
    assert "(a held proposal from agent:agent-x, approved by api_key:key-ci)" in text


def test_sites_is_one_request_and_extra_sites_are_repeated_scope_values(monkeypatch):
    body = {"decision_id": "d1", "at": "2026-10-15T00:00:00+00:00", "all_sites": False, "all_sites_from": None, "rows": []}
    tools, seen = _tools(monkeypatch, lambda *a: body)
    tools["decision_sites"](
        decision_id="d1", at="2026-10-15T00:00:00Z", scopes=[{"site": "05"}, {"site": "06"}]
    )
    assert len(seen) == 1
    method, path, kwargs = seen[0]
    assert (method, path) == ("GET", "/memory/decisions/d1/sites")
    assert kwargs["params"] == {"at": "2026-10-15T00:00:00Z", "scope": ['{"site":"05"}', '{"site":"06"}']}


def test_sites_refuses_a_malformed_extra_site_before_any_request(monkeypatch):
    tools, seen = _tools(monkeypatch, lambda *a: {})
    with pytest.raises(Exception):
        tools["decision_sites"](decision_id="d1", scopes=[{"site": "*"}])
    assert seen == []


def test_history_sends_only_what_is_set_and_404_is_not_found(monkeypatch):
    tools, seen = _tools(monkeypatch, lambda *a: {"status_code": 404, "error": "API error 404"})
    assert tools["decision_history"](decision_id="nope") == "Decision not found: nope"
    assert seen[0][2] == {}
    tools["decision_history"](decision_id="nope", limit=5, before="2026-11-01T00:00:00Z")
    assert seen[1][2]["params"] == {"limit": 5, "before": "2026-11-01T00:00:00Z"}
    tools["decision_history"](decision_id="nope", limit=2, cursor="abc=")
    assert seen[2][2]["params"] == {"limit": 2, "cursor": "abc="}


def test_history_hint_tells_the_reader_to_pass_the_cursor():
    page = {"entries": [], "truncated": True, "next_cursor": "tok=="}
    assert "cursor set to tok==" in history_text("d0", page)
    assert "before set to its date" in history_text("d0", {"entries": [], "truncated": True})


def test_a_bad_instant_is_refused_naming_it(monkeypatch):
    tools, seen = _tools(monkeypatch, lambda *a: {})
    with pytest.raises(Exception, match="before|as_of|ISO"):
        tools["decision_history"](decision_id="d0", before="last tuesday")
    assert seen == []


def test_history_preserves_service_failure_that_mentions_404(monkeypatch):
    tools, _ = _tools(monkeypatch, lambda *args: {
        "status_code": 503, "error": "API error 503: upstream API error 404"
    })
    with pytest.raises(RuntimeError, match="API error 503"):
        tools["decision_history"](decision_id="d0")
