"""Reader presets through registered MCP tools and real Lite/HTTP transports."""

import asyncio

import httpx
import pytest
from fastmcp import Client, FastMCP

from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import lite_context
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import common, memory_tools


@pytest.fixture(scope="module")
def memory(tmp_path_factory):
    with lite_context(
        str(tmp_path_factory.mktemp("test_g8_mcp_")),
        PipelineConfig.lite(llm_enabled=False),
    ) as mem:
        mem.add_learning("test_g8_mcp: inspect the rollback", category="ops")
        mem.add_constraint("test_g8_mcp: preserve the freeze", domain="release")
        yield mem


@pytest.fixture
def server(monkeypatch, memory):
    backend = LocalBackend()
    backend._mem = memory
    monkeypatch.setattr(common, "_backend", backend)
    mcp = FastMCP("test_g8_mcp")
    memory_tools.register_free(mcp)
    return mcp


def call(server, args):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool("memory_recall_pack", args)
            return result.data

    return asyncio.run(go())


@pytest.mark.parametrize(
    "preset,budget",
    [("brief", 2000), ("resume", 3000), ("review", 4000), ("pane", 600)],
)
def test_local_tool_matches_core_reader(server, memory, preset, budget):
    actual = call(server, {"preset": preset})
    expected = memory.recall_pack(preset=preset)
    assert actual == expected
    assert actual["manifest"]["budget_tokens"] == budget
    assert "test_g8_mcp: preserve the freeze" in actual["block"]


def test_remote_tool_expands_to_existing_wire_shape(server, memory, monkeypatch):
    # Real httpx transport, deterministic service-boundary response over the real Lite assembler.
    # No live service, credentials, socket or port is used.
    import json

    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == "/memory/recall/pack"
        assert "preset" not in body
        assert body["budget_tokens"] == 700
        assert [s["name"] for s in body["sections"]] == [
            "constraints",
            "learned",
            "tier1_user",
            "notes",
        ]
        assert body["as_of"] == "2020-01-01T00:00:00Z"
        return httpx.Response(200, json=memory.recall_pack(**body))

    backend = RemoteBackend(
        api_url="https://test-g8.invalid", api_key="test_g8_key", team_id="test_g8_ws"
    )
    backend._session["_bootstrapped"] = True
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "request", client.request)
    monkeypatch.setattr(common, "_backend", backend)
    try:
        pack = call(
            server,
            {"preset": "review", "budget_tokens": 700, "as_of": "2020-01-01T00:00:00Z"},
        )
    finally:
        client.close()
    assert len(seen) == 1
    assert pack["manifest"]["preset"] == "review"
    assert pack["manifest"]["budget_tokens"] == 700
    assert pack["manifest"]["dropped_sections"] == ["constraints", "learned"]


@pytest.mark.parametrize(
    "args",
    [
        {"preset": "breif"},
        {"preset": "pane", "sections": []},
        {"preset": "brief", "budget_tokens": 0},
        {},
    ],
)
def test_invalid_reader_returns_error_without_backend_read(server, args):
    assert "error" in call(server, args)


def test_core_free_preset_table_matches_core():
    from smartmemory.recall_pack import PRESETS, PRESET_BUDGETS
    from smartmemory_mcp import recall_presets

    assert recall_presets.PRESET_BUDGETS == PRESET_BUDGETS
    assert recall_presets.PRESETS == {name: PRESETS[name] for name in PRESET_BUDGETS}


def test_hosted_reader_uses_authenticated_wire_and_real_pack(memory, monkeypatch):
    import json

    from .test_hosted_tools_allowlist import _Harness

    harness = _Harness()
    seen = []

    def handle(request):
        assert request.url.path == "/memory/recall/pack"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["x-workspace-id"]
        body = json.loads(request.content)
        seen.append(body)
        assert "preset" not in body
        return httpx.Response(200, json=memory.recall_pack(**body))

    with httpx.Client(transport=httpx.MockTransport(handle)) as service:
        monkeypatch.setattr(httpx, "request", service.request)

        async def body(client):
            session_id = await harness.session(client)
            return await harness.call(
                client, session_id, "memory_recall_pack", {"preset": "pane"}
            )

        result = harness.run(body)

    assert not result.get("isError"), result
    pack = result["structuredContent"]
    assert pack["manifest"]["preset"] == "pane"
    assert pack["manifest"]["budget_tokens"] == 600
    assert "test_g8_mcp: preserve the freeze" in pack["block"]
    assert len(seen) == 1
    assert [s["name"] for s in seen[0]["sections"]] == [
        "constraints",
        "active_plan",
        "anchors",
        "last_session",
    ]
