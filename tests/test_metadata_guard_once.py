"""Non-dict backend metadata (MCP-2.0.1 r2): one WARNING per item per call, the item's
content stays, the metadata-dependent lines are omitted, and nothing is invented."""

import logging
from types import SimpleNamespace

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.hosted import effects
from smartmemory_mcp.tools import (
    common,
    dev_tools,
    memory_tools,
    reasoning_tools,
    transcript_tools,
    zettel_tools,
)

BAD = ["garbage", [1, 2], 17]


class _Registrar:
    def __init__(self):
        self.functions = {}

    def tool(self, **kwargs):
        def capture(function):
            self.functions[function.__name__] = function
            return function

        return capture


class _Backend:
    def __init__(self, item):
        self.item = item

    def search(self, *args, **kwargs):
        return [self.item]


def _item(metadata, memory_type="decision"):
    return {
        "item_id": "item-1",
        "memory_type": memory_type,
        "content": "the note body",
        "metadata": metadata,
    }


def _tools(monkeypatch, item, *modules):
    monkeypatch.setattr(common, "_backend", _Backend(item))
    registrar = _Registrar()
    for module in modules:
        getattr(module, "register_free", None) and module.register_free(registrar)
        getattr(module, "register", None) and module.register(registrar)
    return registrar.functions


def _warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "item-1" in r.getMessage()
    ]


@pytest.mark.parametrize("metadata", BAD)
def test_catalog_warns_exactly_once(monkeypatch, caplog, metadata):
    tools = _tools(monkeypatch, _item(metadata, "semantic"), memory_tools)
    with caplog.at_level(logging.WARNING):
        out = tools["memory_search"](query="q", catalog_mode=True)
    assert len(_warnings(caplog)) == 1
    assert "the note body" in out and "stale" not in out


@pytest.mark.parametrize("metadata", BAD)
def test_effects_warns_exactly_once(monkeypatch, caplog, metadata):
    class _B:
        def request(self, *a, **k):
            return {"items": [_item(metadata)], "total": 1}

        def supports(self, *a):
            return True

    monkeypatch.setattr(common, "_backend", _B())
    registrar = _Registrar()
    effects.register(registrar)
    with caplog.at_level(logging.WARNING):
        out = registrar.functions["code_effects"](
            repo="r", source_snapshot="sha256:" + "a" * 64
        )
    assert len(_warnings(caplog)) == 1
    assert out["items"][0]["metadata"] == {}


@pytest.mark.parametrize("metadata", BAD)
def test_dev_decisions_warns_once_and_invents_nothing(monkeypatch, caplog, metadata):
    tools = _tools(monkeypatch, _item(metadata), dev_tools)
    with caplog.at_level(logging.WARNING):
        out = tools["dev_query_decisions"](query="q")
    assert len(_warnings(caplog)) == 1
    assert "the note body" in out
    assert "Untitled" not in out and "Date:" not in out


@pytest.mark.parametrize("metadata", BAD)
def test_dev_load_context_warns_once_per_item(monkeypatch, caplog, metadata):
    tools = _tools(monkeypatch, _item(metadata), dev_tools)
    with caplog.at_level(logging.WARNING):
        out = tools["dev_load_context"](topic="q", sessions=1)
    # one item returned by the sessions search, one by the decisions search
    assert len(_warnings(caplog)) == 2
    assert "the note body" in out
    assert "unknown" not in out and "Untitled" not in out and "N/A" not in out


@pytest.mark.parametrize("metadata", BAD)
def test_reasoning_traces_warns_once_and_omits_step_count(
    monkeypatch, caplog, metadata
):
    tools = _tools(monkeypatch, _item(metadata, "reasoning"), reasoning_tools)
    with caplog.at_level(logging.WARNING):
        out = tools["reasoning_query_traces"](query="q")
    assert len(_warnings(caplog)) == 1
    assert "the note body" in out and "steps" not in out


def test_valid_metadata_still_reads(monkeypatch, caplog):
    tools = _tools(
        monkeypatch,
        _item({"title": "T", "recorded_at": "2026-01-01"}),
        dev_tools,
    )
    with caplog.at_level(logging.WARNING):
        out = tools["dev_query_decisions"](query="q")
    assert not _warnings(caplog)
    assert "T" in out and "Date: 2026-01-01" in out


class _Mem:
    def __init__(self, item):
        self.item = item
        self.written = None

    def get(self, item_id):
        return self.item

    def update_properties(self, item_id, props, write_mode=None):
        self.written = props


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("metadata", BAD)
def test_local_update_refuses_malformed_stored_metadata(caplog, metadata, typed):
    item = SimpleNamespace(metadata=metadata) if typed else {"metadata": metadata}
    backend = object.__new__(LocalBackend)
    backend._mem = _Mem(item)
    with caplog.at_level(logging.WARNING):
        out = backend.update("item-1", content="c", metadata={"k": "v"})
    assert out.startswith("Refused:") and "item-1" in out
    assert backend._mem.written is None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) == 1
    assert "item-1" in msgs[0] and type(metadata).__name__ in msgs[0]
    assert "refused" in msgs[0] and "left as it was" in msgs[0]


@pytest.mark.parametrize("typed", [False, True])
def test_local_update_content_only_ignores_malformed_metadata(caplog, typed):
    item = SimpleNamespace(metadata="garbage") if typed else {"metadata": "garbage"}
    backend = object.__new__(LocalBackend)
    backend._mem = _Mem(item)
    with caplog.at_level(logging.WARNING):
        out = backend.update("item-1", content="c")
    assert out == "Updated: item-1"
    assert backend._mem.written == {"content": "c"}
    assert not caplog.records


def test_local_update_merges_dict_metadata_silently(caplog):
    backend = object.__new__(LocalBackend)
    backend._mem = _Mem({"metadata": {"a": 1}})
    with caplog.at_level(logging.WARNING):
        backend.update("item-1", metadata={"k": "v"})
    assert backend._mem.written == {"metadata": {"a": 1, "k": "v"}}
    assert not caplog.records


@pytest.mark.parametrize("metadata", BAD)
def test_zettel_discover_malformed_note_metadata_warns_and_keeps_id(
    monkeypatch, caplog, metadata
):
    zettel = SimpleNamespace(
        discovery=SimpleNamespace(
            discover_missing_connections=lambda _id: [("n2", 0.9)]
        ),
        get=lambda _id: SimpleNamespace(metadata=metadata),
    )
    monkeypatch.setattr(zettel_tools, "_get_zettel_memory", lambda: (zettel, None))
    registrar = _Registrar()
    zettel_tools.register(registrar)
    with caplog.at_level(logging.WARNING):
        out = registrar.functions["zettel_discover"](note_id="n1")
    assert "n2 (surprise: 0.90)" in out
    assert [r for r in caplog.records if "n2" in r.getMessage()]


@pytest.mark.parametrize("metadata", BAD)
def test_transcript_item_meta_never_raises(caplog, metadata):
    item = SimpleNamespace(item_id="t-1", metadata=metadata, origin="mcp:x", cwd="/w")
    with caplog.at_level(logging.WARNING):
        meta = transcript_tools._item_meta(item)
    assert meta["origin"] == "mcp:x" and meta["cwd"] == "/w"
    assert len([r for r in caplog.records if "t-1" in r.getMessage()]) == 1
