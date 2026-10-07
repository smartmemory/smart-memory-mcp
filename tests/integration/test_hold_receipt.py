"""Held receipts through real core storage and the consumer boundary."""

from uuid import uuid4

import pytest
from redis import Redis
from smartmemory.pipeline import PipelineConfig
from smartmemory.tools.factory import create_server_memory
from smartmemory.utils.cache import NoOpCache


@pytest.fixture
def core(monkeypatch):
    name = f"test_core_hold_receipt_{uuid4().hex}"
    redis = Redis(host="localhost", port=9010)
    before = set(redis.execute_command("GRAPH.LIST"))
    cfg = PipelineConfig.graph_only(llm_enabled=False)
    cfg.extraction.entity_ruler.enabled = False
    cfg.extraction.llm_extract.extract_decisions = False
    instance = None
    monkeypatch.setattr("smartmemory.progress._get_redis", lambda: None)
    monkeypatch.setattr(
        "smartmemory.streams.pipeline_producer.emit_pipeline_event", lambda **kw: None
    )
    try:
        instance = create_server_memory(
            graph_name=name,
            pipeline_profile=cfg,
            enable_ontology=False,
            observability=False,
            cache=NoOpCache(),
        )
        yield instance
    finally:
        if instance is not None:
            instance.close()
        if name.encode() in redis.execute_command("GRAPH.LIST"):
            redis.execute_command("GRAPH.DELETE", name)
        keys = list(redis.scan_iter(match=f"sm:vchain:head:{name}:*"))
        if keys:
            redis.delete(*keys)
        assert not list(redis.scan_iter(match=f"sm:vchain:head:{name}:*"))
        assert not {
            k
            for k in set(redis.execute_command("GRAPH.LIST")) - before
            if k.startswith(b"test_core_hold_receipt_")
        }
        redis.close()


def held(core, content="Test knowledge.", **kwargs):
    return core.ingest(content, context={"confidence": 0.4}, sync=True)


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("memory_ingest", {"content": "Test knowledge."}),
        ("dev_save_session", {"summary": "Test knowledge."}),
        ("memory_add", {"content": "Test knowledge."}),
        (
            "memory_distill",
            {"user_turn": "Test knowledge.", "assistant_turn": "Understood."},
        ),
        (
            "dev_record_decision",
            {
                "title": "Test",
                "context": "Test",
                "decision": "Test",
                "rationale": "Test",
            },
        ),
        ("dev_record_pattern", {"name": "Test", "description": "Test knowledge."}),
        (
            "dev_log_friction",
            {"description": "Test knowledge.", "category": "tool_failure"},
        ),
    ],
)
def test_tools_report_real_hold_without_success(core, monkeypatch, name, kwargs):
    from types import SimpleNamespace
    from smartmemory_mcp.tools import memory_tools, dev_tools

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self, **kw):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn

            return register

    backend = SimpleNamespace(
        ingest=lambda content, **kw: held(core, content),
        add=lambda content, **kw: held(core, content),
    )
    for module in (memory_tools, dev_tools):
        monkeypatch.setattr(module, "get_backend", lambda: backend)
    mcp = MCP()
    memory_tools.register_free(mcp)
    memory_tools.register_pro(mcp)
    dev_tools.register(mcp)
    text = mcp.tools[name](**kwargs)
    assert "held" in text.lower() and "low_confidence" in text
    assert not any(
        word in text.lower()
        for word in (
            "saved",
            "ingested",
            "logged",
            "none",
            "sync",
            "stored",
            "recorded",
            "added",
        )
    )
    assert core._graph.backend._query("MATCH (n:Item) RETURN n") == []


def test_remote_add_returns_real_hold(core, monkeypatch):
    from smartmemory_mcp.backends.remote import RemoteBackend

    backend = RemoteBackend.__new__(RemoteBackend)
    monkeypatch.setattr(backend, "_request", lambda *a, **kw: held(core))
    result = backend.add("Test knowledge.", use_pipeline=True)
    assert (
        result["status"] == "held"
        and result["reason"] == "low_confidence"
        and result["item_id"] is None
    )


def test_conversation_formatter_preserves_real_chunk_hold(core, monkeypatch):
    import asyncio
    from dataclasses import asdict
    from types import SimpleNamespace
    from smartmemory.conversation.bulk_ingest import ingest_conversation
    from smartmemory_mcp.tools import memory_tools

    response = asyncio.run(
        ingest_conversation(
            core,
            [{"speaker": "Tester", "content": "Test knowledge."}],
            context={"confidence": 0.4},
        )
    )

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self, **kw):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn

            return register

    for value in [response, asdict(response)]:
        backend = SimpleNamespace(ingest_conversation_sync=lambda **kw: value)
        monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
        mcp = MCP()
        memory_tools.register_pro(mcp)
        text = mcp.tools["memory_ingest_conversation"](turns=[])
        assert (
            "0 chunks ingested, 0 failed" in text and "1 held: low_confidence" in text
        )
