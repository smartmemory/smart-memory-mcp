"""SM-ASOF-DECISIONS-1: ``as_of`` on the dated MCP tools, in both modes.

- Remote: each tool sends ``as_of`` on the exact route the service declares (query param on the GETs, body field on
  ``POST /memory/recall/pack``), and sends nothing new without it.
- Local: a dated read goes through the SmartMemory facade (which binds its history read gate), never a bare
  ``DecisionQueries``/``DecisionManager``; an undated read is the call shipped before.
- An unparseable ``as_of`` is a ``ToolError`` naming the value before any backend call.
- Local mode on a real Lite store answers the D1 -> D2 -> retract headline the way the service does
  (``smart-memory-service/tests/integration/api/test_decisions_as_of.py``).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend

T = "2026-01-15T00:00:00Z"
DECISION = {
    "decision_id": "dec_1",
    "content": "Use Postgres",
    "decision_type": "choice",
    "confidence": 0.7,
}


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools():
    from smartmemory_mcp.tools import decision_tools, memory_tools

    mcp = _FakeMCP()
    decision_tools.register(mcp)
    memory_tools.register_free(mcp)
    return mcp.tools


def _use(backend, monkeypatch):
    from smartmemory_mcp.tools import decision_tools, memory_tools

    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)


def _remote(monkeypatch):
    calls = []

    def transport(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/provenance"):
            return {"decision": DECISION, "superseded": [], "superseded_by": []}
        if path == "/memory/decisions" or path == "/memory/decisions/search":
            return {"decisions": [DECISION], "count": 1}
        if path == "/memory/policy/bundle":
            return {"workspace_id": "ws", "rules": [], "as_of": T}
        if path == "/memory/recall/pack":
            return {"block": "", "manifest": {"as_of": T}}
        return DECISION

    backend = RemoteBackend.__new__(RemoteBackend)
    backend._request = transport
    _use(backend, monkeypatch)
    return calls


def test_remote_sends_as_of_on_every_dated_route(monkeypatch):
    calls = _remote(monkeypatch)
    tools = _tools()

    tools["decision_list"](as_of=T)
    tools["decision_search"]("db", as_of=T)
    tools["decision_get"]("dec_1", as_of=T)
    tools["decision_provenance"]("dec_1", as_of=T)
    tools["memory_policy_bundle"](as_of=T)
    tools["memory_recall_pack"](300, as_of=T)

    sent = {(method, path): kwargs for method, path, kwargs in calls}
    assert sent[("GET", "/memory/decisions")]["params"]["as_of"] == T
    assert sent[("GET", "/memory/decisions/search")]["params"] == {
        "topic": "db",
        "limit": 20,
        "as_of": T,
    }
    assert sent[("GET", "/memory/decisions/dec_1")]["params"] == {"as_of": T}
    assert sent[("GET", "/memory/decisions/dec_1/provenance")]["params"] == {"as_of": T}
    assert sent[("GET", "/memory/policy/bundle")]["params"] == {"as_of": T}
    assert sent[("POST", "/memory/recall/pack")]["json"] == {
        "budget_tokens": 300,
        "as_of": T,
    }


def test_remote_without_as_of_sends_the_request_shipped_before(monkeypatch):
    calls = _remote(monkeypatch)
    tools = _tools()

    tools["decision_list"]()
    tools["decision_get"]("dec_1")
    tools["decision_provenance"]("dec_1")
    tools["memory_policy_bundle"]()
    tools["memory_recall_pack"](300)

    sent = {(method, path): kwargs for method, path, kwargs in calls}
    assert "as_of" not in sent[("GET", "/memory/decisions")]["params"]
    assert "params" not in sent[("GET", "/memory/decisions/dec_1")]
    assert "params" not in sent[("GET", "/memory/decisions/dec_1/provenance")]
    assert sent[("GET", "/memory/policy/bundle")]["params"] == {}
    assert sent[("POST", "/memory/recall/pack")]["json"] == {"budget_tokens": 300}


@pytest.mark.parametrize(
    "call",
    [
        lambda tools: tools["decision_list"](as_of="not-a-time"),
        lambda tools: tools["decision_search"]("db", as_of="not-a-time"),
        lambda tools: tools["decision_get"]("dec_1", as_of="not-a-time"),
        lambda tools: tools["decision_provenance"]("dec_1", as_of="not-a-time"),
        lambda tools: tools["memory_policy_bundle"](as_of="not-a-time"),
        lambda tools: tools["memory_recall_pack"](300, as_of="not-a-time"),
    ],
)
def test_unparseable_as_of_is_a_tool_error_naming_the_value(monkeypatch, call):
    calls = _remote(monkeypatch)

    with pytest.raises(ToolError, match="not-a-time"):
        call(_tools())
    assert calls == []


def test_local_dated_reads_go_through_the_facade(monkeypatch):
    decision = MagicMock()
    decision.to_dict.return_value = dict(DECISION)
    mem = MagicMock()
    mem.get_decision.return_value = decision
    mem.get_active_decisions.return_value = [decision]
    mem.get_decisions_about.return_value = [decision]
    mem.get_decision_provenance.return_value = {
        "decision": decision,
        "superseded": [],
        "superseded_by": [],
    }
    mem.compile_policy_bundle.return_value = {"rules": []}
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = mem
    queries = MagicMock(
        side_effect=AssertionError(
            "a dated read must not bypass the facade's history gate"
        )
    )
    monkeypatch.setattr(backend, "_decision_queries", queries, raising=False)
    monkeypatch.setattr(backend, "_decision_manager", queries, raising=False)

    backend.decision_get("dec_1", as_of=T)
    backend.decision_list(as_of=T)
    backend.decision_search("db", as_of=T)
    backend.decision_provenance("dec_1", as_of=T)
    backend.policy_bundle(as_of=T)

    assert mem.get_decision.call_args.kwargs == {"as_of": T}
    assert mem.get_active_decisions.call_args.kwargs["as_of"] == T
    assert mem.get_decisions_about.call_args.kwargs["as_of"] == T
    assert mem.get_decision_provenance.call_args.kwargs == {"as_of": T}
    assert mem.compile_policy_bundle.call_args.kwargs["as_of"] == T
    backend.policy_bundle()
    assert "as_of" not in mem.compile_policy_bundle.call_args.kwargs


def _instant() -> str:
    time.sleep(0.02)
    stamp = datetime.now(timezone.utc).isoformat()
    time.sleep(0.02)
    return stamp


@pytest.fixture
def lite_backend(tmp_path):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    memory = create_lite_memory(
        data_dir=str(tmp_path),
        pipeline_profile=PipelineConfig.lite(llm_enabled=False),
        spawn_worker=False,
    )
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = memory
    try:
        yield backend
    finally:
        memory._graph.backend.close()


def test_local_mode_headline_on_a_real_lite_store(lite_backend):
    from smartmemory.models.decision import Decision

    mem = lite_backend._mem
    t0 = _instant()
    d1 = mem.add_decision("Use Postgres for the spine database").decision_id
    t1 = _instant()
    d2 = mem.supersede_decision(
        d1, Decision(content="Use SQLite for the spine database"), reason="cost"
    ).decision_id
    t2 = _instant()
    mem.retract_decision(d2, reason="undecided")
    t3 = _instant()

    def ids(rows):
        return sorted(row["decision_id"] for row in rows)

    assert ids(lite_backend.decision_list(as_of=t0)) == []
    assert ids(lite_backend.decision_list(as_of=t1)) == [d1]
    assert ids(lite_backend.decision_list(as_of=t2)) == [d2]
    assert ids(lite_backend.decision_list(as_of=t3)) == []
    assert ids(lite_backend.decision_list()) == []
    assert lite_backend.decision_get(d1, as_of=t1)["status"] == "active"
    assert lite_backend.decision_get(d2, as_of=t3)["status"] == "retracted"
    assert ids(lite_backend.decision_search("spine database", as_of=t1)) == [d1]
    assert ids(lite_backend.decision_search("spine database", as_of=t2)) == [d2]
    assert lite_backend.decision_provenance(d1, as_of=t1)["superseded_by"] == []
    assert ids(lite_backend.decision_provenance(d1, as_of=t2)["superseded_by"]) == [d2]
