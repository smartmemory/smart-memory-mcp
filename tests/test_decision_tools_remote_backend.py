"""MCP-REMOTE-DECISIONS-1 — every decision tool works in remote mode.

Before this feature the twelve write/read tools plus ``decision_try_activate``
returned a refusal string whenever the server ran against the hosted service, so an
agent on the primary product configuration could not record a decision at all. They
now forward to ``/memory/decisions/*``.

Two things are pinned here, both of which a refactor could quietly undo:

1. **Route and payload.** Each tool hits the exact path and body the service route
   declares (``smart-memory-service/memory_service/api/routes/decisions.py``); a typo
   in a path would otherwise surface only as a live 404.
2. **Loud failure** (no-silent-degradation). An API error becomes a RuntimeError
   naming the status and the route. A 500 on a list must never render as
   "No active decisions found." and a failed create must never look like a success.
"""

import re

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools():
    from smartmemory_mcp.tools import decision_tools

    mcp = _FakeMCP()
    decision_tools.register(mcp)
    return mcp.tools


class _Transport:
    """Records requests and replays canned responses in RemoteBackend._request shape."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        response = self.responses
        if callable(response):
            return response(method, path, kwargs)
        return response


def _remote(responses):
    backend = RemoteBackend.__new__(RemoteBackend)
    transport = _Transport(responses)
    backend._request = transport
    return backend, transport


def _run(tool_name, backend, monkeypatch, **kwargs):
    from smartmemory_mcp.tools import decision_tools

    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    return _tools()[tool_name](**kwargs)


DECISION = {
    "decision_id": "dec_1",
    "content": "Use Postgres for the ledger",
    "decision_type": "choice",
    "confidence": 0.7,
    "status": "active",
    "rejected_alternatives": ["DynamoDB"],
    "rationale": "ACID and team familiarity",
    "constraints": ["single region"],
    "ignorance": 1.0,
}


# --- Routes and payloads -----------------------------------------------------------


def test_create_posts_the_service_contract_body(monkeypatch):
    backend, transport = _remote(
        {
            "decision_id": "dec_1",
            "decision_type": "choice",
            "confidence": 0.7,
            "rationale": "ACID and team familiarity",
            "rejected_alternatives": ["DynamoDB"],
            "constraints": ["single region"],
            # The route splats the scope context into its response; none of it
            # may reach an MCP client.
            "user_id": "user_x",
            "workspace_id": "team_x",
            "tenant_id": "tenant_x",
        }
    )
    out = _run(
        "decision_create",
        backend,
        monkeypatch,
        content="Use Postgres for the ledger",
        decision_type="choice",
        confidence=0.7,
        rationale="ACID and team familiarity",
        rejected_alternatives=["DynamoDB"],
        constraints=["single region"],
    )

    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("POST", "/memory/decisions/create")
    assert kwargs["json"]["content"] == "Use Postgres for the ledger"
    assert kwargs["json"]["decision_type"] == "choice"
    assert kwargs["json"]["confidence"] == 0.7
    assert out.startswith("Decision created: dec_1")
    assert "user_x" not in out and "team_x" not in out


def test_get_uses_the_id_route(monkeypatch):
    backend, transport = _remote(DECISION)
    out = _run("decision_get", backend, monkeypatch, decision_id="dec_1")
    assert transport.calls[0][:2] == ("GET", "/memory/decisions/dec_1")
    assert "dec_1" in out and "ignorance" in out


def test_get_reports_a_404_as_absence(monkeypatch):
    backend, _ = _remote({"error": "API error 404: Decision not found"})
    out = _run("decision_get", backend, monkeypatch, decision_id="dec_missing")
    assert out == "Decision not found: dec_missing"


def test_list_unwraps_the_decisions_envelope(monkeypatch):
    backend, transport = _remote({"decisions": [DECISION], "count": 1})
    out = _run("decision_list", backend, monkeypatch, domain="arch", limit=10)
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("GET", "/memory/decisions")
    assert kwargs["params"] == {"min_confidence": 0.0, "limit": 10, "domain": "arch"}
    assert "Found 1 active decisions" in out
    assert "[dec_1] (choice, conf=0.70)" in out


def test_list_carries_the_uncertainty_marker_into_remote_mode(monkeypatch):
    """CORE-DECISION-BELIEF-SURFACE-1 must not be a local-only nicety."""
    backend, _ = _remote({"decisions": [DECISION], "count": 1})
    out = _run("decision_list", backend, monkeypatch)
    assert "not enough evidence" in out


def test_search_uses_the_search_route(monkeypatch):
    backend, transport = _remote(
        {"decisions": [DECISION], "count": 1, "topic": "ledger"}
    )
    out = _run("decision_search", backend, monkeypatch, topic="ledger", limit=5)
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("GET", "/memory/decisions/search")
    assert kwargs["params"] == {"topic": "ledger", "limit": 5}
    assert "Found 1 decisions about 'ledger'" in out


def test_supersede_posts_the_replacement(monkeypatch):
    backend, transport = _remote(
        {
            "old_decision_id": "dec_1",
            "new_decision_id": "dec_2",
            "status": "superseded",
            "user_id": "user_x",
        }
    )
    out = _run(
        "decision_supersede",
        backend,
        monkeypatch,
        decision_id="dec_1",
        new_content="Use CockroachDB",
        reason="multi-region",
        new_confidence=0.95,
        rejected_alternatives=["Keep Citus"],
        rationale="Regional failover is required",
        constraints=["No manual operator"],
    )
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("POST", "/memory/decisions/dec_1/supersede")
    assert kwargs["json"] == {
        "new_content": "Use CockroachDB",
        "new_decision_type": "inference",
        "new_confidence": 0.95,
        "reason": "multi-region",
        "rejected_alternatives": ["Keep Citus"],
        "rationale": "Regional failover is required",
        "constraints": ["No manual operator"],
    }
    assert out == "Decision dec_1 superseded.\nNew decision: dec_2"
    assert "user_x" not in out


def test_retract_posts_the_reason(monkeypatch):
    backend, transport = _remote({"decision_id": "dec_1", "status": "retracted"})
    out = _run(
        "decision_retract", backend, monkeypatch, decision_id="dec_1", reason="wrong"
    )
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("POST", "/memory/decisions/dec_1/retract")
    assert kwargs["json"] == {"reason": "wrong"}
    assert out == "Decision retracted: dec_1"


def test_reinforce_renders_the_new_counts(monkeypatch):
    backend, transport = _remote(
        {"decision_id": "dec_1", "confidence": 0.955, "reinforcement_count": 1}
    )
    out = _run(
        "decision_reinforce",
        backend,
        monkeypatch,
        decision_id="dec_1",
        evidence_id="mem_1",
    )
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("POST", "/memory/decisions/dec_1/reinforce")
    assert kwargs["json"] == {"evidence_id": "mem_1"}
    assert out == (
        "Decision reinforced: dec_1\nNew confidence: 0.95\nReinforcement count: 1"
    )


def test_contradict_renders_the_new_counts(monkeypatch):
    backend, transport = _remote(
        {"decision_id": "dec_1", "confidence": 0.81, "contradiction_count": 2}
    )
    out = _run(
        "decision_contradict",
        backend,
        monkeypatch,
        decision_id="dec_1",
        evidence_id="mem_1",
    )
    assert transport.calls[0][:2] == ("POST", "/memory/decisions/dec_1/contradict")
    assert out == (
        "Decision contradicted: dec_1\nNew confidence: 0.81\nContradiction count: 2"
    )


def test_provenance_renders_the_chain(monkeypatch):
    backend, transport = _remote(
        {
            "decision": DECISION,
            "reasoning_trace": None,
            "evidence": [{"memory": {}}],
            "superseded": [DECISION],
        }
    )
    out = _run("decision_provenance", backend, monkeypatch, decision_id="dec_1")
    assert transport.calls[0][:2] == ("GET", "/memory/decisions/dec_1/provenance")
    assert out == (
        "Provenance for dec_1:\n"
        "Decision: Use Postgres for the ledger\n"
        "Evidence: 1 items\n"
        "Superseded: 1 decisions"
    )


def test_find_conflicts_posts_to_the_conflicts_route(monkeypatch):
    backend, transport = _remote(
        {"decision_id": "dec_1", "conflicts": [DECISION], "count": 1}
    )
    out = _run("decision_find_conflicts", backend, monkeypatch, decision_id="dec_1")
    assert transport.calls[0][:2] == ("POST", "/memory/decisions/dec_1/conflicts")
    assert "Found 1 conflicts for dec_1" in out
    assert "[dec_1] (choice)" in out


def test_find_conflicts_reports_absence_not_an_empty_list(monkeypatch):
    backend, _ = _remote({"error": "API error 404: Decision not found"})
    out = _run("decision_find_conflicts", backend, monkeypatch, decision_id="dec_x")
    assert out == "Decision not found: dec_x"


def test_create_pending_posts_the_requirements(monkeypatch):
    backend, transport = _remote(
        {
            "decision_id": "dec_p",
            "status": "pending",
            "pending_requirements": [
                {
                    "requirement_id": "req_abc12345",
                    "description": "load test passes",
                    "requirement_type": "proof",
                    "resolved": False,
                }
            ],
        }
    )
    out = _run(
        "decision_create_pending",
        backend,
        monkeypatch,
        content="Ship the ledger",
        requirements=[{"description": "load test passes", "requirement_type": "proof"}],
        domain="gtm",
    )
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("POST", "/memory/decisions/pending/create")
    assert kwargs["json"]["requirements"][0]["description"] == "load test passes"
    assert kwargs["json"]["domain"] == "gtm"
    assert "Pending decision created: dec_p" in out
    assert "req_abc12345: load test passes [proof] resolved=False" in out


def test_resolve_requirement_posts_to_the_pending_route(monkeypatch):
    backend, transport = _remote({"resolved": True, "decision": None})
    out = _run(
        "decision_resolve_requirement",
        backend,
        monkeypatch,
        decision_id="dec_p",
        requirement_id="req_1",
        memory_id="mem_1",
    )
    method, path, kwargs = transport.calls[0]
    assert (method, path) == ("POST", "/memory/decisions/pending/dec_p/resolve")
    assert kwargs["json"] == {"requirement_id": "req_1", "memory_id": "mem_1"}
    assert out == "Requirement req_1 resolved on dec_p"


def test_resolve_requirement_reports_a_404_as_not_found(monkeypatch):
    """The route 404s for an unknown requirement — that is the local False, not an outage."""
    backend, _ = _remote({"error": "API error 404: Requirement req_x not found"})
    out = _run(
        "decision_resolve_requirement",
        backend,
        monkeypatch,
        decision_id="dec_p",
        requirement_id="req_x",
        memory_id="mem_1",
    )
    assert out == "Requirement req_x NOT FOUND on dec_p"


def test_try_activate_posts_to_the_activate_route(monkeypatch):
    backend, transport = _remote({"activated": False, "decision": None})
    out = _run("decision_try_activate", backend, monkeypatch, decision_id="dec_p")
    assert transport.calls[0][:2] == (
        "POST",
        "/memory/decisions/pending/dec_p/activate",
    )
    assert out == "Decision dec_p still pending (unresolved requirements remain)"


# --- Loud failure ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("decision_create", {"content": "x"}),
        ("decision_get", {"decision_id": "dec_1"}),
        ("decision_list", {}),
        ("decision_search", {"topic": "x"}),
        (
            "decision_supersede",
            {"decision_id": "dec_1", "new_content": "y", "reason": "z"},
        ),
        ("decision_retract", {"decision_id": "dec_1", "reason": "z"}),
        ("decision_reinforce", {"decision_id": "dec_1", "evidence_id": "m"}),
        ("decision_contradict", {"decision_id": "dec_1", "evidence_id": "m"}),
        ("decision_provenance", {"decision_id": "dec_1"}),
        ("decision_find_conflicts", {"decision_id": "dec_1"}),
        (
            "decision_create_pending",
            {"content": "x", "requirements": [{"description": "d"}]},
        ),
        (
            "decision_resolve_requirement",
            {"decision_id": "dec_p", "requirement_id": "r", "memory_id": "m"},
        ),
        ("decision_try_activate", {"decision_id": "dec_p"}),
    ],
)
def test_every_tool_raises_on_an_api_error(monkeypatch, tool, kwargs):
    """A 500 must never render as an empty result or a cheerful success line."""
    backend, _ = _remote({"error": "API error 500: boom"})
    with pytest.raises(RuntimeError) as excinfo:
        _run(tool, backend, monkeypatch, **kwargs)
    message = str(excinfo.value)
    assert "500" in message, message
    assert re.search(r"/memory/decisions\S*", message), message


def test_an_empty_response_body_is_an_error_not_a_success(monkeypatch):
    backend, _ = _remote(None)
    with pytest.raises(RuntimeError, match="empty response"):
        _run("decision_create", backend, monkeypatch, content="x")
