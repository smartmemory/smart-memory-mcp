"""Regression tests for MCP-RECALL-PROFILE-SILENT-NOOP-1.

`agent_set_recall_profile` used to store the profile as a *procedural memory item* in the
graph. Search applies the profile from the service's agent record (MongoDB), so the written
profile was never read by anything — and `agent_get_recall_profile` read the same dead item
back, which made the round-trip look correct. A silent no-op that reported success.

These tests pin the store: the profile goes to the authoritative REST endpoint, and local
mode (where nothing consumes a recall profile at all) refuses instead of faking success.
"""

import pytest

from smartmemory_mcp.tools import agent_tools


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


class _RemoteBackend:
    """Has `.request` — the REST escape hatch RemoteBackend exposes."""

    def __init__(self, get_response=None):
        self.calls = []
        self._get_response = get_response or {"agent_id": "a1", "recall_profile": {}}

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json")))
        if method == "PUT":
            return {
                "agent_id": "a1",
                "recall_profile": kwargs["json"]["recall_profile"],
            }
        return self._get_response

    def add(self, *a, **k):
        raise AssertionError("recall profile must not be written as a memory item")

    def search(self, *a, **k):
        raise AssertionError("recall profile must not be read from the graph")


class _LocalBackend:
    """No `.request` — local mode."""

    def add(self, *a, **k):
        raise AssertionError("local mode must not write a dead-store profile")

    def search(self, *a, **k):
        raise AssertionError("local mode must not read a dead-store profile")


def _tools(monkeypatch, backend):
    monkeypatch.setattr(agent_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    agent_tools.register(mcp)
    return mcp.fns


def test_set_writes_to_the_authoritative_rest_endpoint(monkeypatch):
    backend = _RemoteBackend()
    fns = _tools(monkeypatch, backend)

    out = fns["agent_set_recall_profile"]("a1", {"decision": 2.0})

    assert backend.calls == [
        (
            "PUT",
            "/memory/agents/a1/recall-profile",
            {"recall_profile": {"memory_type_weights": {"decision": 2.0}}},
        ),
    ]
    assert "decision: 2.0x" in out


def test_get_reads_the_authoritative_rest_endpoint(monkeypatch):
    backend = _RemoteBackend(
        get_response={
            "agent_id": "a1",
            "recall_profile": {"memory_type_weights": {"decision": 2.0}},
        }
    )
    fns = _tools(monkeypatch, backend)

    out = fns["agent_get_recall_profile"]("a1")

    assert backend.calls == [("GET", "/memory/agents/a1/recall-profile", None)]
    assert "decision" in out


def test_get_reports_no_profile_when_unset(monkeypatch):
    fns = _tools(monkeypatch, _RemoteBackend())
    assert "no recall profile" in fns["agent_get_recall_profile"]("a1")


def test_api_error_is_surfaced_not_swallowed(monkeypatch):
    class _Failing(_RemoteBackend):
        def request(self, method, path, **kwargs):
            return {"error": "API error 404: Agent not found"}

    fns = _tools(monkeypatch, _Failing())
    out = fns["agent_set_recall_profile"]("a1", {"decision": 2.0})
    assert "Failed to set recall profile" in out and "404" in out


@pytest.mark.parametrize(
    "tool", ["agent_set_recall_profile", "agent_get_recall_profile"]
)
def test_local_mode_refuses_instead_of_faking_success(monkeypatch, tool):
    """Nothing on the local search path reads a recall profile, so claiming success is a lie."""
    fns = _tools(monkeypatch, _LocalBackend())
    args = ("a1", {"decision": 2.0}) if tool == "agent_set_recall_profile" else ("a1",)

    out = fns[tool](*args)  # @graceful turns NotImplementedError into its message

    assert "not supported in local mode" in out
