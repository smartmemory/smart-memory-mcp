"""memory_get returns a compact metadata summary by default (MCP-MEMGET-META-1)."""

import json
import logging
from pathlib import Path

import anyio
import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import common, memory_tools
from smartmemory_mcp.tools.metadata_summary import summarize_item_metadata

from ._hosted_fixtures import hosted_config, memory_store

FIXTURE = Path(__file__).parent / "fixtures" / "memget_a3_8f64f81f.json"

BOOKKEEPING_VALUES = (
    "tenant_id",
    "owner_key",
    "security_scope",
    "created_by",
    "activation",
    "retrieval",
    "existing_fact",
    "new_fact",
    "ontology_registry_id",
    "discoverability_score",
    "workspace_id",
    "team_id",
    "user_id",
)


class FakeBackend:
    def __init__(self, item):
        self.item = item

    def get(self, item_id, **kwargs):
        return self.item


def _item(metadata, **extra):
    return {
        "item_id": "item-1",
        "memory_type": "semantic",
        "content": "the note body",
        "metadata": metadata,
        **extra,
    }


def _memory_get(monkeypatch, item):
    monkeypatch.setattr(common, "_backend", FakeBackend(item))
    registrar = _CapturingRegistrar(FastMCP("test_memget_local"))
    memory_tools.register_free(registrar)
    return registrar.captured["memory_get"].function


def _bookkeeping_metadata():
    return {
        "tenant_id": "tenant_SECRET",
        "workspace_id": "ws_SECRET",
        "user_id": "user_SECRET",
        "owner_key": "owner_SECRET",
        "security_scope": "workspace",
        "activation": {"score": 0.9},
        "retrieval": {"profile": {"name": "PROFILE_SECRET"}},
        "challenge_result": {
            "conflicts": [
                {
                    "existing_item_id": "other-9",
                    "existing_fact": "EXISTING_FACT_BODY",
                    "new_fact": "NEW_FACT_BODY",
                    "conflict_type": "direct_contradiction",
                    "explanation": "[HEURISTIC] negation pattern",
                }
            ]
        },
        "created_at": "2026-10-09T05:53:07+00:00",
        "source": "docs/bill.txt",
        "tags": ["alpha", "beta"],
    }


def test_default_output_is_compact_summary(monkeypatch):
    out = _memory_get(monkeypatch, _item(_bookkeeping_metadata()))("item-1")
    assert out.startswith("Memory Item: item-1\nType: semantic\nContent: the note body")
    assert (
        "conflicts with other-9: direct_contradiction: [HEURISTIC] negation pattern"
        in out
    )
    assert "Created: 2026-10-09T05:53:07+00:00" in out
    assert "Source: docs/bill.txt" in out
    assert "Tags: alpha, beta" in out
    for secret in (
        "tenant_SECRET",
        "ws_SECRET",
        "user_SECRET",
        "owner_SECRET",
        "PROFILE_SECRET",
        "EXISTING_FACT_BODY",
        "NEW_FACT_BODY",
        "activation",
        "retrieval",
        "Metadata:",
    ):
        assert secret not in out


def test_include_metadata_reproduces_full_dump(monkeypatch):
    meta = _bookkeeping_metadata()
    out = _memory_get(monkeypatch, _item(meta))("item-1", include_metadata=True)
    assert out == "\n".join(
        [
            "Memory Item: item-1",
            "Type: semantic",
            "Content: the note body",
            f"Metadata: {meta}",
        ]
    )
    for key in meta:
        assert key in out


def test_include_metadata_with_empty_metadata_has_no_metadata_line(monkeypatch):
    out = _memory_get(monkeypatch, _item({}))("item-1", include_metadata=True)
    assert "Metadata" not in out


@pytest.mark.parametrize(
    "metadata, expect, absent",
    [
        (None, [], ["conflicts with"]),
        ({}, [], ["conflicts with"]),
        (
            {
                "challenge_result": {
                    "conflicts": [
                        {
                            "existing_item_id": "a",
                            "conflict_type": "t",
                            "explanation": "e",
                        }
                    ]
                }
            },
            ["conflicts with a: t: e"],
            [],
        ),
        (
            {"conflicts": [{"existing_item_id": "b", "conflict_type": "t2"}]},
            ["conflicts with b: t2"],
            [],
        ),
        (
            {
                "challenge_result": {
                    "conflicts": {"c1": {"conflict_type": "t3", "explanation": "x"}}
                }
            },
            ["conflicts with c1: t3: x"],
            [],
        ),
        (
            {
                "challenge_result": {
                    "conflicts": {
                        "conflicts": [{"existing_item_id": "d", "conflict_type": "t4"}]
                    }
                }
            },
            ["conflicts with d: t4"],
            [],
        ),
        ({"challenge_result": {"conflicts": "garbage"}}, [], ["conflicts with"]),
        ({"challenge_result": {"conflicts": [1, "x", None]}}, [], ["conflicts with"]),
        ({"challenge_result": "garbage"}, [], ["conflicts with"]),
        ({"conflicts": [{"conflict_type": None}]}, ["conflicts with ?: unknown"], []),
        (
            {"resolved_dates": ["2026-01-02"]},
            ["Resolved dates: 2026-01-02"],
            ["Created"],
        ),
        (
            {
                "resolved_dates": [
                    {"text": "x", "date": "2026-01-04", "type": "absolute"}
                ]
            },
            ["Resolved dates: 2026-01-04"],
            ["absolute"],
        ),
        ({"resolved_date": "2026-01-03"}, ["Resolved date: 2026-01-03"], ["Created"]),
        (
            {"origin": "user", "provenance": "import"},
            ["Origin: user", "Provenance: import"],
            [],
        ),
        ({"source": {"nested": "dict"}, "tags": "notalist"}, [], ["Source", "Tags"]),
    ],
)
def test_edge_rows(monkeypatch, metadata, expect, absent):
    out = _memory_get(monkeypatch, _item(metadata))("item-1")
    for text in expect:
        assert text in out
    for text in absent:
        assert text not in out
    assert "Content: the note body" in out


def test_long_explanation_is_truncated(monkeypatch):
    meta = {
        "conflicts": [
            {"existing_item_id": "z", "conflict_type": "t", "explanation": "w" * 5000}
        ]
    }
    out = _memory_get(monkeypatch, _item(meta))("item-1")
    line = next(l for l in out.splitlines() if l.startswith("conflicts with z"))
    assert len(line) < 220 and line.endswith("...")


def test_many_conflicts_are_capped(monkeypatch):
    meta = {
        "conflicts": [
            {"existing_item_id": f"i{n}", "conflict_type": "t"} for n in range(50)
        ]
    }
    out = _memory_get(monkeypatch, _item(meta))("item-1")
    assert out.count("conflicts with i") == 20
    assert "(+30 more conflicts)" in out


def test_valid_dates_from_item_fields(monkeypatch):
    item = _item(
        {}, created_at="2026-01-01", valid_start_time="2026-02-01", valid_end_time=None
    )
    out = _memory_get(monkeypatch, item)("item-1")
    assert "Created: 2026-01-01" in out and "Valid: 2026-02-01 to open" in out


@pytest.mark.parametrize("bad", ["a string", ["a", "list"], 42])
def test_malformed_metadata_logs_warning_and_does_not_raise(monkeypatch, caplog, bad):
    with caplog.at_level(logging.WARNING):
        out = _memory_get(monkeypatch, _item(bad))("item-1")
    assert "Content: the note body" in out
    assert any(
        r.levelno == logging.WARNING and "not a dict" in r.getMessage()
        for r in caplog.records
    )


def test_section_failure_degrades_with_warning(caplog):
    class Boom(dict):
        def get(self, key, default=None):
            raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        lines = summarize_item_metadata({"item_id": "x", "metadata": Boom()})
    assert lines == []
    assert any("failed" in r.getMessage() for r in caplog.records)


def test_a3_real_note_default_is_under_quarter_of_old_size(monkeypatch):
    fixture = json.loads(FIXTURE.read_text())
    old = _memory_get(monkeypatch, fixture)("x", include_metadata=True)
    new = _memory_get(monkeypatch, fixture)("x")
    assert len(old) > 25000
    assert len(new) < 0.25 * len(old), (len(old), len(new))
    assert fixture["content"] in new
    listed = fixture["metadata"]["challenge_result"]["conflicts"]
    assert new.count("conflicts with ") == len(listed) == 3
    for key in BOOKKEEPING_VALUES:
        assert key not in new
    assert "Created: 2026-10-09T05:53:07.452072+00:00" in new


@pytest.fixture
def _restore_hosted_mode():
    before = hosted_identity.hosted_mode_enabled()
    try:
        yield
    finally:
        hosted_identity.set_hosted_mode(before)


def test_hosted_and_local_servers_expose_include_metadata(_restore_hosted_mode):
    from smartmemory_mcp.server import mcp as local

    server = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    async def hosted_tool():
        async with Client(server) as client:
            return next(t for t in await client.list_tools() if t.name == "memory_get")

    hosted = anyio.run(hosted_tool)
    hosted_identity.set_hosted_mode(False)
    from smartmemory_mcp.server import mcp as local_server

    async def local_tool():
        return await local_server.get_tool("memory_get")

    local = anyio.run(local_tool)
    tools = {"hosted": hosted, "local": local}
    for label, tool in tools.items():
        schema = getattr(tool, "input_schema", None) or tool.parameters
        props = schema["properties"]
        assert props["include_metadata"]["type"] == "boolean", label
        assert props["include_metadata"]["default"] is False, label
        assert schema["required"] == ["item_id"], label
        assert "include_metadata" in tool.description, label
        assert "memory_explain" in tool.description, label
