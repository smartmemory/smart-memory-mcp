"""SM-LOCAL-TOOLS-1: the fourteen tools MCP-LOCAL-BROKEN-TOOLS-1 hid in local mode work there.

One contract test per tool. A real Lite store (``create_lite_memory`` under a temp dir, no worker),
seeded through core's own managers; every PRO+ tool registered the way ``server.py`` does it,
behind the real capability middleware; each tool called through FastMCP. Each test asserts what the
store holds or the ids the tool returned, never just "no error".

Mechanism tests pin where each fix lives: the Class A managers are built on ``LocalBackend._mem``
(the real SmartMemory), never the wrapper (the ``test_decision_tools_local_backend.py`` rule).
"""

from __future__ import annotations

import asyncio
import json
import tempfile

import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.backends.interface import LOCAL_BROKEN_TOOLS
from smartmemory_mcp.backends.local import LOCAL_WORKSPACE, LocalBackend
from smartmemory_mcp.capabilities import BackendCapabilityMiddleware
from smartmemory_mcp.tier import Tier
from smartmemory_mcp.tools import common

pytest.importorskip("smartmemory.tools.factory")

from smartmemory.adherence import PatternManager  # noqa: E402
from smartmemory.anchors.manager import AnchorManager  # noqa: E402
from smartmemory.decisions.manager import DecisionManager  # noqa: E402
from smartmemory.memory.types.zettel_memory import ZettelMemory  # noqa: E402
from smartmemory.models.memory_item import MemoryItem  # noqa: E402
from smartmemory.tools.factory import create_lite_memory  # noqa: E402

# The fourteen tools MCP-LOCAL-BROKEN-TOOLS-1 hid in local mode.
FIXED = (
    "memory_search_advanced",
    "memory_policy_bundle",
    "pattern_list",
    "pattern_query",
    "memory_plan_active",
    "memory_anchor_list",
    "memory_anchor_check_drift",
    "dev_load_context",
    "zettel_clusters",
    "zettel_backlinks",
    "zettel_connections",
    "zettel_discover",
    "memory_plan_create",
    "memory_log_failure",
)


@pytest.fixture(scope="module")
def mem():
    with tempfile.TemporaryDirectory(prefix="test_sm_local_tools_") as data_dir:
        memory = create_lite_memory(data_dir=data_dir, spawn_worker=False)
        try:
            yield memory
        finally:
            memory.close()


@pytest.fixture
def server(monkeypatch, mem):
    """A local-mode FastMCP over ``mem``: every PRO+ tool, the real capability middleware."""
    import smartmemory_mcp.server as server_module

    backend = LocalBackend()
    backend._mem = mem
    mcp = FastMCP("test_sm_local_tools")
    mcp.add_middleware(BackendCapabilityMiddleware())
    monkeypatch.setattr(common, "_backend", backend)
    # LocalBackend.search reads through smartmemory_app.storage's own singleton; pin it to this
    # store so no tool here can reach the user's real ~/.smartmemory.
    monkeypatch.setattr(
        "smartmemory_app.storage.get_memory", lambda *args, **kwargs: mem
    )
    monkeypatch.setattr(server_module, "mcp", mcp)
    monkeypatch.setattr(server_module, "resolve_tier", lambda: Tier.PRO_PLUS)
    monkeypatch.setattr(server_module, "_TRANSCRIPT_TOOLS_REGISTERED", False)
    server_module._register_tools()
    return mcp


def call(mcp, tool, args=None):
    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(tool, args or {})
            return result.data if result.data is not None else result.content

    return asyncio.run(go())


def listed(mcp):
    async def go():
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(go())


def nodes(mem, **query):
    return mem._graph.backend.search_nodes(query)


# -- Headline ------------------------------------------------------------------------------------


def test_no_tool_is_hidden_and_all_fourteen_are_listed_locally(server):
    assert LOCAL_BROKEN_TOOLS == {}
    assert set(FIXED) <= listed(server)


# -- Class A: core managers on the real store -----------------------------------------------------


def test_pattern_list_returns_the_seeded_pattern(server, mem):
    seeded = PatternManager(mem).create(
        content="Prefer TaskGroup",
        title="test_pattern_list_title",
        severity="error",
        scope=["**/*.py"],
        examples=[],
    )
    rows = call(server, "pattern_list")
    assert {(row["pattern_id"], row["title"]) for row in rows} >= {
        (seeded.pattern_id, "test_pattern_list_title")
    }


def test_pattern_query_filters_to_the_seeded_pattern(server, mem):
    seeded = PatternManager(mem).create(
        content="Quote shell vars",
        title="test_pattern_query_title",
        severity="warn",
        scope=["**/*.sh"],
        examples=[],
    )
    rows = call(server, "pattern_query", {"free_text": "test_pattern_query_title"})
    assert [(row["pattern_id"], row["title"]) for row in rows] == [
        (seeded.pattern_id, "test_pattern_query_title")
    ]


def test_plan_create_with_tasks_then_plan_active_returns_it(server, mem):
    text = call(
        server,
        "memory_plan_create",
        {
            "title": "test_plan_title",
            "tasks": [
                {"content": "test_task_a"},
                {"content": "test_task_b", "depends_on": [0]},
            ],
        },
    )
    plan_id = text.split("plan_id: ")[1].split(",")[0]
    assert text.endswith("tasks: 2")
    # The plan and both task nodes are in the store; the dependency resolved to the first task's id.
    assert [n["item_id"] for n in nodes(mem, memory_type="plan", plan_id=plan_id)] == [
        plan_id
    ]
    tasks = {
        n["content"]: n for n in nodes(mem, memory_type="plan_task", plan_id=plan_id)
    }
    assert set(tasks) == {"test_task_a", "test_task_b"}
    assert tasks["test_task_b"]["depends_on"] == [tasks["test_task_a"]["item_id"]]
    active = call(server, "memory_plan_active")
    assert f"{plan_id}: test_plan_title (0/2 tasks)" in active
    plan = json.loads(call(server, "memory_plan_get", {"plan_id": plan_id}))
    assert {t["content"] for t in plan["tasks"]} == {"test_task_a", "test_task_b"}


def test_anchor_list_returns_the_seeded_anchor(server, mem):
    anchor_id = AnchorManager(mem).set(
        "test_anchor_list keep the API stable", "spec", "test_session_list"
    )
    text = call(server, "memory_anchor_list", {"session_id": "test_session_list"})
    assert text.startswith("Active anchors (1):")
    assert f"[spec] {anchor_id}: test_anchor_list keep the API stable" in text


def test_anchor_check_drift_names_the_seeded_anchor(server, mem):
    anchor_id = AnchorManager(mem).set(
        "postgres migrations stay reversible", "constraint", "test_session_drift"
    )
    text = call(
        server,
        "memory_anchor_check_drift",
        {
            "session_id": "test_session_drift",
            "recent_outputs": ["rewrote the frontend colours"],
        },
    )
    assert text.startswith("Drift report (1 anchor(s)):")
    assert f"  {anchor_id}: severity=" in text
    assert "anchor: postgres migrations stay reversible" in text


def test_log_failure_stores_an_agent_failure_and_check_failure_finds_it(server, mem):
    text = call(
        server,
        "memory_log_failure",
        {
            "error_type": "test_err_type",
            "content": "pip install timed out",
            "context": "pip install of test deps offline",
        },
    )
    item_id = text.removeprefix("Failure logged. Item ID: ")
    stored = mem.get(item_id)
    assert stored is not None and stored.memory_type == "episodic"
    assert stored.origin == "failure:agent"
    assert stored.metadata["error_type"] == "test_err_type"
    # The MemoryItem reached core whole: the content is the failure text, not a repr of the item.
    assert stored.content == "pip install timed out"
    found = call(
        server,
        "memory_check_failure",
        {"error_type": "test_err_type", "context": "pip install of test deps offline"},
    )
    assert found.startswith("Found 1 prior failure(s):")
    assert "[test_err_type] pip install timed out" in found


# -- Class E: a decision with rationale None ------------------------------------------------------


def test_dev_load_context_lists_a_decision_without_rationale(server, mem):
    decision = DecisionManager(mem).create(
        content="test_devctx use sqlite for the local cache",
        decision_type="preference",
        confidence=0.9,
        rationale=None,
    )
    assert mem.get(decision.decision_id).metadata.get("rationale") is None
    text = call(
        server,
        "dev_load_context",
        {"topic": "test_devctx use sqlite for the local cache"},
    )
    assert "=== Related Decisions (" in text
    assert "No related decisions found." not in text


# -- Class B: zettels on the Lite graph -----------------------------------------------------------


@pytest.fixture(scope="module")
def zettels(mem):
    """Three linked notes (a -> b -> c) and an unlinked one (d), all tagged ``test_kb``."""
    for note_id, title in (
        ("test_zet_a", "Alpha"),
        ("test_zet_b", "Beta"),
        ("test_zet_c", "Gamma"),
        ("test_zet_d", "Delta"),
    ):
        mem.add(
            MemoryItem(
                content=f"{title} note",
                item_id=note_id,
                memory_type="zettel",
                metadata={"title": title, "tags": ["test_kb"]},
            )
        )
    zettel = ZettelMemory.for_memory(mem)
    zettel.create_bidirectional_link("test_zet_a", "test_zet_b")
    zettel.create_bidirectional_link("test_zet_b", "test_zet_c")
    return zettel


def test_zettel_backlinks_name_the_linking_note(server, zettels):
    text = call(server, "zettel_backlinks", {"note_id": "test_zet_b"})
    assert text.startswith("Backlinks for test_zet_b (1):")
    assert "id=test_zet_a" in text


def test_zettel_connections_name_both_linked_notes(server, zettels):
    text = call(server, "zettel_connections", {"note_id": "test_zet_b"})
    assert "test_zet_a" in text and "test_zet_c" in text
    assert "forward_link" in text and "backlink" in text


def test_zettel_clusters_return_the_linked_notes(server, zettels):
    text = call(server, "zettel_clusters", {"min_size": 3})
    assert text.startswith("Found 1 knowledge clusters:")
    assert "4 notes" in text  # the shared tag joins test_zet_d to the a-b-c chain


def test_zettel_discover_returns_unlinked_notes_sharing_a_tag(server, zettels):
    text = call(server, "zettel_discover", {"note_id": "test_zet_a"})
    assert text.startswith("Discoveries for test_zet_a (2):")
    # test_zet_b is already linked from test_zet_a; the two unlinked notes sharing the tag come back.
    assert "Gamma (surprise: 1.00)" in text and "Delta (surprise: 1.00)" in text
    assert "Beta" not in text


# -- Class C: SSG through the facade --------------------------------------------------------------


def test_search_advanced_returns_the_seeded_item(server, mem):
    item_id = mem.add(
        MemoryItem(
            content="test_ssg the lighthouse keeper logs every passing ship",
            memory_type="semantic",
        )
    )
    text = call(
        server, "memory_search_advanced", {"query": "lighthouse keeper logs ships"}
    )
    assert f"[{item_id}]" in text


# -- Class D: Lite's one workspace ----------------------------------------------------------------


def test_policy_bundle_is_compiled_for_lites_default_workspace(server, mem):
    manager = DecisionManager(mem)
    ruled = manager.create(
        content="test_policy run the linter",
        decision_type="policy",
        confidence=0.9,
        constraints=["run ruff before commit"],
    )
    manager.create(
        content="test_policy no constraint", decision_type="preference", confidence=0.9
    )
    bundle = call(server, "memory_policy_bundle")
    assert bundle["workspace_id"] == LOCAL_WORKSPACE == "default"
    rules = {rule["rule_id"]: rule for rule in bundle["rules"]}
    assert f"{ruled.decision_id}#0" in rules
    assert (
        rules[f"{ruled.decision_id}#0"]["predicate"]["judged"]["statement"]
        == "run ruff before commit"
    )
    assert {rule["source"]["record_id"] for rule in bundle["rules"]} == {
        d["item_id"]
        for d in nodes(mem, memory_type="decision", status="active")
        if d.get("constraints")
    }


# -- Mechanism: Class A managers receive the real SmartMemory --------------------------------------


@pytest.mark.parametrize(
    ("factory", "target"),
    [
        ("_pattern_manager", "smartmemory.adherence.PatternManager"),
        ("_plan_manager", "smartmemory.plans.manager.PlanManager"),
        ("_anchor_manager", "smartmemory.anchors.manager.AnchorManager"),
        ("_failure_journal", "smartmemory.plans.failure_journal.FailureJournal"),
    ],
)
def test_class_a_managers_are_built_on_the_real_store_not_the_wrapper(
    monkeypatch, factory, target
):
    captured = {}

    class Recorder:
        def __init__(self, memory):
            captured["memory"] = memory

    monkeypatch.setattr(target, Recorder)
    backend = LocalBackend()
    real = object()
    backend._mem = real
    getattr(backend, factory)()
    assert captured["memory"] is real and captured["memory"] is not backend


# -- Remote: the two reads that reached the service keep that path -------------------------------


@pytest.mark.parametrize(
    ("method", "args", "target"),
    [
        ("plan_get", ("test_plan",), "smartmemory.plans.manager.PlanManager"),
        (
            "check_failure",
            ("test_err", "test_ctx"),
            "smartmemory.plans.failure_journal.FailureJournal",
        ),
    ],
)
def test_remote_reads_that_reached_the_service_still_build_on_the_remote_backend(
    monkeypatch, method, args, target
):
    from smartmemory_mcp.backends.remote import RemoteBackend

    captured = {}

    class Recorder:
        def __init__(self, memory):
            captured["memory"] = memory

        def get(self, item_id):
            captured["get"] = item_id

        def check_before_retry(self, error_type, context, top_k):
            captured["check"] = (error_type, context, top_k)
            return []

    monkeypatch.setattr(target, Recorder)
    remote = RemoteBackend(api_key="sk_test_local_tools")
    getattr(remote, method)(*args)
    assert captured["memory"] is remote
    assert captured.get("get") == args[0] or captured.get("check") == (*args, 3)


def test_remote_refuses_the_rest_by_name():
    from smartmemory_mcp.backends.remote import RemoteBackend

    remote = RemoteBackend(api_key="sk_test_local_tools")
    # Patterns: REST since SM-PATTERN-CRUD-1 (tests/test_pattern_write_tools.py).
    for method in (
        "plan_create",
        "anchor_list",
        "anchor_check_drift",
        "log_failure",
    ):
        with pytest.raises(NotImplementedError, match="Not available in remote mode"):
            getattr(remote, method)()
