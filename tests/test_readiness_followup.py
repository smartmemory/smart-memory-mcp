"""REL-2.0-CUT-1 regressions: real Lite storage and a loopback HTTP fault harness."""

import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading

from fastmcp import Client, FastMCP
import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS
from smartmemory_mcp.tools import memory_tools

SCHEDULE = [{"scope": {"site": "A"}, "valid_from": "2026-01-01T00:00:00+00:00"}]
CONTENT = "Evidence " * 60 + "END OF FULL EVIDENCE"


@pytest.fixture
def local(tmp_path):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    memory = create_lite_memory(
        data_dir=str(tmp_path / "test_mcp_readiness"),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    )
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = memory
    try:
        yield backend
    finally:
        memory.close()


def server(backend, monkeypatch):
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
    mcp = FastMCP("test_mcp_readiness")
    memory_tools.register_free(mcp)
    memory_tools.register_pro(mcp)
    return mcp


def test_local_scheduled_receipt_matches_persisted_amendment(local):
    from smartmemory.models.memory_item import MemoryItem

    old = local._mem.add(MemoryItem(content="Old rule", memory_type="semantic"))
    result = local.supersede(old, "New rule", "amended", scope_go_lives=SCHEDULE)
    assert result["status"] == "scheduled"
    assert result["scope_go_lives"] == SCHEDULE
    assert datetime.fromisoformat(result["recorded_at"]).tzinfo is not None
    replacement = local._mem.get(result["new_item_id"])
    assert replacement.content == "New rule"
    assert replacement.metadata["scope_go_lives"] == SCHEDULE
    assert not local._mem.get(old).metadata.get("superseded")
    edges = local._mem._graph.backend.get_edges_for_node(old)
    assert any(
        e["edge_type"] == "ROLLOUT_NEXT" and e["target_id"] == result["new_item_id"]
        for e in edges
    )
    # The original published scalar-date call also schedules when history is scoped.
    inferred = local.supersede(
        result["new_item_id"],
        "Later rule",
        "later amendment",
        valid_from="2026-02-01T00:00:00+00:00",
    )
    assert inferred["status"] == "scheduled"
    assert inferred["scope_go_lives"] == [
        {"scope": None, "valid_from": "2026-02-01T00:00:00+00:00"}
    ]
    assert datetime.fromisoformat(inferred["recorded_at"]).tzinfo is not None


def test_tool_reports_scheduled_for_dated_supersede_of_scoped_history(
    local, monkeypatch
):
    """DRIFT-V3-DHRUV-BOUNDARIES-FU-1 F1: real Lite scoped history, then a future valid_from."""
    from smartmemory.models.memory_item import MemoryItem

    old = local._mem.add(MemoryItem(content="Old rule", memory_type="semantic"))
    first = local.supersede(old, "Sponsor rule", "go-live", scope_go_lives=SCHEDULE)
    assert first["status"] == "scheduled"
    mcp = server(local, monkeypatch)

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "memory_supersede",
                {
                    "item_id": first["new_item_id"],
                    "content": "Later rule",
                    "reason": "future amendment",
                    "valid_from": "2099-01-01T00:00:00Z",
                },
            )
            text = " ".join(c.text for c in result.content if hasattr(c, "text"))
            return json.loads(text)

    receipt = asyncio.run(run())
    assert receipt["status"] == "scheduled"
    assert receipt["old_item_id"] == first["new_item_id"]
    assert receipt["new_item_id"] != first["new_item_id"]
    assert receipt["scope_go_lives"] == [
        {"scope": None, "valid_from": "2099-01-01T00:00:00+00:00"}
    ]
    assert datetime.fromisoformat(receipt["recorded_at"]).tzinfo is not None


def test_tool_plain_supersede_text_unchanged(local, monkeypatch):
    from smartmemory.models.memory_item import MemoryItem

    old = local._mem.add(MemoryItem(content="Plain rule", memory_type="semantic"))
    mcp = server(local, monkeypatch)

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "memory_supersede",
                {"item_id": old, "content": "Plain v2", "reason": "changed"},
            )
            return " ".join(c.text for c in result.content if hasattr(c, "text"))

    text = asyncio.run(run())
    assert text.startswith(f"Superseded: {old} -> ")


@pytest.fixture
def wire(local):
    """Exercise real HTTP framing and dropped acknowledgements without a provider."""
    seen = []
    committed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body, dict(self.headers)))
            if self.path == "/memory/add":
                from smartmemory.models.memory_item import MemoryItem

                item_id = local._mem.add(
                    MemoryItem(
                        content=body["content"],
                        memory_type=body["memory_type"],
                        origin="test:readiness",
                    )
                )
                committed.append(item_id)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.path.endswith("/supersede"):
                result = {
                    "status": "scheduled",
                    "old_item_id": "old",
                    "new_item_id": "new",
                    "scope_go_lives": SCHEDULE,
                    "recorded_at": "2026-11-06T00:00:00+00:00",
                }
            elif self.path == "/memory/search":
                result = {
                    "results": [
                        {
                            "item_id": f"test_readiness_{i}",
                            "content": CONTENT,
                            "memory_type": "semantic",
                            "confidence": 1.0,
                        }
                        for i in range(body["top_k"])
                    ]
                }
            elif self.path == "/memory/ask":
                result = {
                    "answer": "Known answer",
                    "reasoning": "",
                    "evidence": [],
                    "relations": [],
                }
            else:
                self.send_error(404)
                return
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    http = HTTPServer(("127.0.0.1", 19084), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    backend = RemoteBackend(
        api_url="http://127.0.0.1:19084",
        api_key="test_readiness_key",
        team_id="test_readiness_ws",
    )
    backend._session["_bootstrapped"] = True
    try:
        yield backend, seen, committed
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_tool_preserves_remote_scheduled_receipt(wire, monkeypatch):
    backend, seen, _ = wire
    mcp = server(backend, monkeypatch)

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "memory_supersede",
                {"item_id": "old", "content": "new", "reason": "amended"},
            )
            text = " ".join(c.text for c in result.content if hasattr(c, "text"))
            receipt = json.loads(text)
            assert receipt == {
                "status": "scheduled",
                "old_item_id": "old",
                "new_item_id": "new",
                "scope_go_lives": SCHEDULE,
                "recorded_at": "2026-11-06T00:00:00+00:00",
            }

    asyncio.run(run())
    assert seen[0][2]["Authorization"] == "Bearer test_readiness_key"
    assert seen[0][2]["X-Workspace-Id"] == "test_readiness_ws"


def test_published_ask_uses_authenticated_workspace_route(wire, monkeypatch):
    backend, seen, _ = wire
    mcp = server(backend, monkeypatch)

    async def run():
        async with Client(mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "memory_ask" in names
            assert "memory_ask" in HOSTED_TOOLS
            result = await client.call_tool(
                "memory_ask",
                {"question": "What is known?", "limit": 7, "reasoning": False},
            )
            assert "Known answer" in str(result)

    asyncio.run(run())
    path, body, headers = seen[0]
    assert path == "/memory/ask"
    assert body == {"question": "What is known?", "limit": 7, "reasoning": False}
    assert headers["Authorization"] == "Bearer test_readiness_key"
    assert headers["X-Workspace-Id"] == "test_readiness_ws"


def test_recall_retains_query_budget_and_full_content(wire):
    backend, seen, _ = wire
    result = backend.recall("relevant rule", top_k=6)
    assert [(path, body["query"], body["top_k"]) for path, body, _ in seen] == [
        ("/memory/search", "relevant rule", 6)
    ]
    assert result.count(CONTENT) == 6


def test_lost_add_receipt_is_typed_and_never_retried(wire, local):
    backend, seen, committed = wire
    with pytest.raises(RuntimeError) as caught:
        backend.add_with_receipt("test_readiness persisted before disconnect")
    assert type(caught.value).__name__ == "WriteOutcomeUnknown", str(caught.value)
    assert "may have been saved" in str(caught.value)
    assert "before retrying" in str(caught.value)
    assert len(committed) == 1
    assert (
        local._mem.get(committed[0]).content
        == "test_readiness persisted before disconnect"
    )
    assert len(seen) == 1
