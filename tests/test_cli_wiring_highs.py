"""Regression probes for the process MCP and CLI wiring audit. No network or stores."""

import asyncio
import runpy
import sys
from types import ModuleType

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from scripts import hosted_abuse_probe
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import (
    agent_tools,
    anchor_tools,
    common,
    dev_tools,
    evolution_tools,
    insight_tools,
    memory_tools,
    plan_tools,
    reasoning_tools,
    zettel_tools,
)


@pytest.fixture(scope="module")
def registry():
    server = FastMCP("wiring-audit")
    for module in (
        agent_tools,
        anchor_tools,
        dev_tools,
        evolution_tools,
        insight_tools,
        plan_tools,
        reasoning_tools,
        zettel_tools,
    ):
        module.register(server)
    memory_tools.register_pro(server)
    memory_tools.register_feedback(server)
    return server


def call(registry, tool, *args):
    return asyncio.run(registry.get_tool(tool)).fn(*args)


@pytest.fixture
def remote(monkeypatch):
    backend = object.__new__(RemoteBackend)

    def refuse_request(*args, **kwargs):
        pytest.fail("unexpected network request")

    monkeypatch.setattr(backend, "_request", refuse_request)
    monkeypatch.setattr(common, "_backend", backend)
    return backend


REMOTE_ONLY_FAILURES = {
    "agent_evaluation_get": ("audit", "decision_volume", "test"),
    "memory_anchor_set": ("audit",),
    "memory_anchor_list": (),
    "memory_anchor_clear": (),
    "memory_anchor_graduate": (),
    "memory_anchor_check_drift": (),
    "evolution_trigger": (),
    "evolution_synthesize_opinions": (),
    "evolution_synthesize_observations": (),
    "evolution_reinforce_opinions": (),
    "clustering_run": (),
    "insight_health": (),
    "insight_reflect": (),
    "insight_maintenance_status": (),
    "insight_personalize": (),
    "insight_feedback": (),
    "insight_ground": ("audit-note", "urn:audit"),
    "memory_search_advanced": ("audit",),
    "memory_plan_create": ("audit",),
    "memory_plan_active": (),
    "memory_plan_update_task": ("audit", "task", "complete"),
    "memory_log_failure": ("audit", "test", "test"),
    "reasoning_challenge": ("audit",),
    "reasoning_resolve_conflict": ("audit", "test"),
    "reasoning_proof_tree": ("audit",),
    "reasoning_fuzzy_confidence": ("audit",),
    "zettel_backlinks": ("audit",),
    "zettel_connections": ("audit",),
    "zettel_clusters": (),
    "zettel_discover": ("audit",),
}


@pytest.mark.parametrize("tool", REMOTE_ONLY_FAILURES)
def test_missing_backend(registry, remote, tool):
    with pytest.raises(ToolError, match=f"not available in this backend: {tool}"):
        value = call(registry, tool, *REMOTE_ONLY_FAILURES[tool])
        pytest.fail(f"successful tool result: {value!r}")


@pytest.mark.parametrize(
    "tool,args",
    [
        ("dev_save_session", ("audit",)),
        ("dev_log_friction", ("audit", "tool_failure")),
        ("evolution_status", ()),
    ],
    ids=["dev_save_session", "dev_log_friction", "evolution_status"],
)
def test_backend_failure(registry, remote, monkeypatch, tool, args):
    monkeypatch.setattr(remote, "_request", lambda *a, **kw: {"error": "audit outage"})
    if tool == "evolution_status":

        def search_outage(*args, **kwargs):
            raise ConnectionError("audit outage")

        monkeypatch.setattr(remote, "search", search_outage)
    with pytest.raises(ToolError, match="audit outage"):
        value = call(registry, tool, *args)
        pytest.fail(f"successful tool result: {value!r}")


def test_feedback_without_search(registry, monkeypatch):
    monkeypatch.setattr(common, "_backend", LocalBackend())
    with pytest.raises(ToolError, match="search context"):
        value = call(registry, "memory_feedback", "missing", [])
        pytest.fail(f"successful tool result: {value!r}")


def test_module_launcher(monkeypatch):
    calls = []
    server = ModuleType("smartmemory_mcp.server")
    server.main = lambda: calls.append("main")
    monkeypatch.setitem(sys.modules, server.__name__, server)
    runpy.run_module("smartmemory_mcp", run_name="__main__")
    assert calls == ["main"]


def test_transport_error_exit(monkeypatch, capsys):
    async def failed_run(*args):
        return [hosted_abuse_probe.Sample("transport_error", 1)]

    monkeypatch.setattr(hosted_abuse_probe, "_run", failed_run)
    monkeypatch.setattr(sys, "argv", ["probe", "--base", "http://localhost"])
    assert hosted_abuse_probe.main() == 1
    assert "transport" in capsys.readouterr().err.lower()


def test_feedback_rejects_superseded_session(registry, monkeypatch):
    from smartmemory_app import storage

    backend = LocalBackend()
    monkeypatch.setattr(common, "_backend", backend)
    monkeypatch.setattr(storage, "search", lambda query, *a, **kw: [{"item_id": query}])
    backend.search("first")
    first = backend._last_search_session_id
    backend.search("second")
    assert first != backend._last_search_session_id
    with pytest.raises(ToolError, match="no matching search context"):
        call(registry, "memory_feedback", first, ["first"])


@pytest.mark.parametrize(
    "tool,args,tag",
    [
        ("dev_save_session", ("audit",), "dev-session"),
        ("dev_log_friction", ("audit", "tool_failure"), "dev-friction"),
    ],
)
def test_dev_ingest_confirmed_with_metadata(
    registry, remote, monkeypatch, tool, args, tag
):
    def accept(method, path, *, json, **kwargs):
        assert (method, path) == ("POST", "/memory/ingest")
        assert tag in json["context"]["tags"]
        return {"item_id": "audit-confirmed"}

    monkeypatch.setattr(remote, "_request", accept)
    assert "audit-confirmed" in call(registry, tool, *args)


def test_protocol_marks_unavailable_as_error(registry, remote):
    from fastmcp import Client

    async def check():
        async with Client(registry) as client:
            result = await client.call_tool(
                "memory_anchor_list", {}, raise_on_error=False
            )
            assert result.is_error
            assert (
                "not available in this backend: memory_anchor_list"
                in result.content[0].text
            )

    asyncio.run(check())
