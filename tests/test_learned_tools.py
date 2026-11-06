"""SM-LEARNED-TOOLS-1 (G7): ``memory_learn`` and ``memory_constrain``.

Local mode runs on a real Lite store (``create_lite_memory`` under a temp dir, no worker): each row writes through the
tool, reads the node back from the graph, and reads it again through ``memory_recall_pack``. Remote mode asserts the
route and the exact body the backend sends (the real-database round trip of the same routes is the service test
``tests/security/test_learned_constraint_routes.py``).
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS
from smartmemory_mcp.tools import common, memory_tools

pytest.importorskip("smartmemory.tools.factory")

from smartmemory.tools.factory import create_lite_memory  # noqa: E402

PACK_SECTIONS = [{"name": "learned"}, {"name": "constraints"}]


@pytest.fixture(scope="module")
def mem():
    with tempfile.TemporaryDirectory(prefix="test_sm_learned_tools_") as data_dir:
        memory = create_lite_memory(data_dir=data_dir, spawn_worker=False)
        try:
            yield memory
        finally:
            memory.close()


@pytest.fixture
def server(monkeypatch, mem):
    """A local-mode FastMCP over the real store: the FREE memory tools, the way ``server.py`` registers them."""
    backend = LocalBackend()
    backend._mem = mem
    mcp = FastMCP("test_sm_learned_tools")
    monkeypatch.setattr(common, "_backend", backend)
    memory_tools.register_free(mcp)
    return mcp


def call(mcp, tool, args=None):
    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(tool, args or {})
            return result.data if result.data is not None else result.content

    return asyncio.run(go())


def nodes(mem, memory_type):
    return mem._graph.backend.search_nodes({"memory_type": memory_type})


def node_for(mem, memory_type, item_id):
    (row,) = [row for row in nodes(mem, memory_type) if row.get("item_id") == item_id]
    return row


def item_id_of(text: str) -> str:
    return text.rsplit(": ", 1)[1].strip()


def pack_block(server) -> str:
    pack = call(
        server, "memory_recall_pack", {"budget_tokens": 4000, "sections": PACK_SECTIONS}
    )
    assert "error" not in pack, pack
    return pack["block"]


# -- Headline: local mode, real store -----------------------------------------------------------------


def test_memory_learn_writes_a_learned_node_with_its_fields(server, mem):
    text = call(
        server,
        "memory_learn",
        {
            "content": "test_learn_fields: never run migrations at peak",
            "category": "ops",
            "incident_link": "INC-7",
            "learned_scope": "repo",
        },
    )
    assert text.startswith("Learned recorded: ")
    row = node_for(mem, "learned", item_id_of(text))
    assert row["content"] == "test_learn_fields: never run migrations at peak"
    assert (row["category"], row["incident_link"], row["learned_scope"]) == (
        "ops",
        "INC-7",
        "repo",
    )


def test_memory_constrain_writes_a_constraint_node_with_its_fields(server, mem):
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0)
    text = call(
        server,
        "memory_constrain",
        {
            "content": "test_constrain_fields: never publish an sdist",
            "domain": "release",
            "source": "owner",
            "expires_at": expires.isoformat(),
        },
    )
    assert text.startswith("Constraint recorded: ")
    row = node_for(mem, "constraint", item_id_of(text))
    assert row["content"] == "test_constrain_fields: never publish an sdist"
    assert (row["domain"], row["source"]) == ("release", "owner")
    assert str(row["expires_at"]).startswith(expires.strftime("%Y-%m-%d"))


def test_a_naive_expires_at_is_read_as_utc(server, mem):
    text = call(
        server,
        "memory_constrain",
        {"content": "test_naive_expiry rule", "expires_at": "2099-01-01T00:00:00"},
    )
    row = node_for(mem, "constraint", item_id_of(text))
    assert str(row["expires_at"]).startswith("2099-01-01")


def test_recall_pack_returns_both_rows_and_drops_an_expired_constraint(server):
    call(
        server,
        "memory_learn",
        {"content": "test_pack_lesson: dry-run every migration", "category": "ops"},
    )
    call(
        server,
        "memory_constrain",
        {"content": "test_pack_live: no ARQ", "domain": "infra"},
    )
    call(
        server,
        "memory_constrain",
        {
            "content": "test_pack_expired: freeze until 2020",
            "domain": "infra",
            "expires_at": "2020-01-01T00:00:00Z",
        },
    )
    block = pack_block(server)
    assert "test_pack_lesson: dry-run every migration" in block
    assert "[ops]" in block
    assert "test_pack_live: no ARQ" in block
    assert "[infra]" in block
    assert "test_pack_expired" not in block


def test_the_expired_constraint_is_still_stored_only_the_pack_filters_it(server, mem):
    text = call(
        server,
        "memory_constrain",
        {"content": "test_expired_stored rule", "expires_at": "2020-01-01T00:00:00Z"},
    )
    assert (
        node_for(mem, "constraint", item_id_of(text))["content"]
        == "test_expired_stored rule"
    )
    assert "test_expired_stored" not in pack_block(server)


# -- Input checks ---------------------------------------------------------------------------------------


def test_an_unknown_learned_scope_is_refused_and_nothing_is_stored(server, mem):
    before = len(nodes(mem, "learned"))
    text = call(
        server,
        "memory_learn",
        {"content": "test_bad_scope lesson", "learned_scope": "planet"},
    )
    assert text.startswith("Error: unknown learned_scope 'planet'")
    assert len(nodes(mem, "learned")) == before


def test_a_bad_expires_at_is_refused_and_nothing_is_stored(server, mem):
    before = len(nodes(mem, "constraint"))
    text = call(
        server,
        "memory_constrain",
        {"content": "test_bad_expiry rule", "expires_at": "next tuesday"},
    )
    assert text.startswith("Error: `expires_at`")
    assert len(nodes(mem, "constraint")) == before


@pytest.mark.parametrize("tool", ["memory_learn", "memory_constrain"])
def test_blank_content_is_refused(server, tool):
    assert call(server, tool, {"content": "   "}).startswith("Error: `content`")


def test_recall_pack_still_refuses_an_unknown_section(server):
    pack = call(
        server,
        "memory_recall_pack",
        {"budget_tokens": 100, "sections": [{"name": "nope"}]},
    )
    assert "unknown section 'nope'" in pack["error"]


# -- No root scope or workspace reaches a write -----------------------------------------------------------


def test_the_tools_take_no_scope_or_workspace_parameter(server):
    async def go():
        async with Client(server) as client:
            return {
                tool.name: set(tool.inputSchema["properties"])
                for tool in await client.list_tools()
            }

    properties = asyncio.run(go())
    assert properties["memory_learn"] == {
        "content",
        "category",
        "incident_link",
        "learned_scope",
    }
    assert properties["memory_constrain"] == {
        "content",
        "domain",
        "source",
        "expires_at",
    }


@pytest.mark.parametrize("method", ["add_learning", "add_constraint"])
@pytest.mark.parametrize("backend_class", [LocalBackend, RemoteBackend])
def test_a_root_scope_or_workspace_is_a_type_error_on_both_backends(
    backend_class, method
):
    backend = backend_class.__new__(backend_class)
    with pytest.raises(TypeError):
        getattr(backend, method)("x", scope="other-workspace")
    with pytest.raises(TypeError):
        getattr(backend, method)("x", workspace_id="other-workspace")


def test_local_backend_builds_on_the_real_store_not_the_wrapper(mem):
    backend = LocalBackend()
    backend._mem = mem
    seen = {}
    real = mem.add_learning

    def spy(content, **kwargs):
        seen["self_is_real"] = True
        return real(content, **kwargs)

    mem.add_learning = spy
    try:
        out = backend.add_learning("test_real_store lesson", category="ops")
    finally:
        del mem.add_learning
    assert seen == {"self_is_real": True}
    assert out["item_id"] == out["learned_id"]
    assert node_for(mem, "learned", out["item_id"])["category"] == "ops"


# -- Remote: the routes and the body --------------------------------------------------------------------


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


def _remote(response):
    backend = RemoteBackend.__new__(RemoteBackend)
    backend._request = _Transport(response)
    return backend


def test_remote_learn_posts_the_contract_body_and_strips_the_scope_echo(
    server, monkeypatch
):
    backend = _remote(
        {
            "item_id": "learned_1",
            "learned_id": "learned_1",
            "content": "c",
            "category": "ops",
            "user_id": "user_x",
            "workspace_id": "ws_x",
            "tenant_id": "tenant_x",
        }
    )
    out = backend.add_learning(
        "c", category="ops", incident_link=None, learned_scope="workspace"
    )
    assert backend._request.calls == [
        (
            "POST",
            "/memory/learned",
            {"json": {"content": "c", "category": "ops", "learned_scope": "workspace"}},
        )
    ]
    assert out == {
        "item_id": "learned_1",
        "learned_id": "learned_1",
        "content": "c",
        "category": "ops",
    }


def test_remote_constrain_posts_the_contract_body(monkeypatch):
    backend = _remote(
        {
            "item_id": "constraint_1",
            "constraint_id": "constraint_1",
            "workspace_id": "ws_x",
        }
    )
    out = backend.add_constraint(
        "c", domain="release", source="owner", expires_at="2030-01-01T00:00:00Z"
    )
    assert backend._request.calls == [
        (
            "POST",
            "/memory/constraints",
            {
                "json": {
                    "content": "c",
                    "domain": "release",
                    "source": "owner",
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            },
        )
    ]
    assert out == {"item_id": "constraint_1", "constraint_id": "constraint_1"}


def test_the_tools_run_on_the_remote_backend_end_to_end(monkeypatch):
    backend = _remote({"item_id": "learned_9", "workspace_id": "ws_x"})
    monkeypatch.setattr(common, "_backend", backend)
    mcp = FastMCP("test_sm_learned_remote")
    memory_tools.register_free(mcp)
    assert (
        call(mcp, "memory_learn", {"content": "c", "category": "ops"})
        == "Learned recorded: learned_9"
    )
    assert backend._request.calls[0][:2] == ("POST", "/memory/learned")


@pytest.mark.parametrize("method", ["add_learning", "add_constraint"])
def test_a_remote_failure_raises_never_reports_success(method):
    backend = _remote({"error": "API error 403: read-only", "status_code": 403})
    with pytest.raises(RuntimeError, match="403"):
        getattr(backend, method)("c")


# -- Hosted -----------------------------------------------------------------------------------------------


def test_both_tools_are_hosted_write_tools():
    assert {"memory_learn", "memory_constrain"} <= HOSTED_TOOLS
