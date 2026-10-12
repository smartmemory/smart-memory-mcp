"""U5.22 local MCP normalizers carry the merged-read extraction status. # trace G:G43,G46,G114,G136,G142

The carrier (`read_status`) exists only on the visibility-gate-ON arm. Every
normalizer must keep it when present and add nothing when absent, so the legacy
arm's output is unchanged.
"""

import logging

import pytest

from smartmemory_mcp.backends.models import (
    MalformedReadStatusError,
    normalize_item,
    normalize_items,
)
from smartmemory_mcp.tools.memory_tools import _build_working_context, model_item

STATUS = {
    "extraction_state": "extraction_pending",
    "processing_stage": "received",
    "execution_state": "queued",
    "outcome": None,
    "revision": 1,
}


class _Obj:
    """Object-with-to_dict, the shape core MemoryItem has."""

    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


class _Attr:
    """Plain object, the getattr fallback shape."""

    item_id = "test_attr"
    content = "atlas"
    read_status = STATUS


def _base(**extra):
    return {"item_id": "test_item", "content": "atlas budget", **extra}


# One row per input shape the normalizer accepts; each has a carrier and a bare twin.
SHAPES = [
    ("dict", lambda e: _base(**e)),
    ("to_dict-object", lambda e: _Obj(_base(**e))),
]


@pytest.mark.parametrize("name,make", SHAPES, ids=[n for n, _ in SHAPES])
def test_normalize_item_keeps_carrier_and_adds_nothing_when_absent(name, make):
    with_status = normalize_item(make({"read_status": STATUS}))
    assert with_status["read_status"] == STATUS
    assert with_status["item_id"] == "test_item"
    bare = normalize_item(make({}))
    assert "read_status" not in bare  # legacy arm output unchanged


def test_normalize_item_attribute_object_keeps_carrier():
    assert normalize_item(_Attr())["read_status"] == STATUS


def test_normalize_item_copies_the_carrier():
    src = dict(STATUS)
    out = normalize_item(_base(read_status=src))
    out["read_status"]["revision"] = 99
    assert src["revision"] == 1


# C8 table: a present-but-unreadable carrier is a typed refusal with exactly one WARNING, never read as absent.
MALFORMED = [
    ("string", "extraction_pending", "a str, not a dict"),
    ("list", ["extraction_pending"], "a list, not a dict"),
    ("int", 7, "a int, not a dict"),
    ("empty-string", "", "a str, not a dict"),
    ("false", False, "a bool, not a dict"),
    ("dict-without-state", {"revision": 1}, "a dict without a string extraction_state"),
    (
        "dict-empty-state",
        {"extraction_state": ""},
        "a dict without a string extraction_state",
    ),
    (
        "dict-int-state",
        {"extraction_state": 3},
        "a dict without a string extraction_state",
    ),
]


def _working_context_site(row):
    class _B:
        def search(self, query, top_k=5, **kwargs):
            return [row]

    return _build_working_context(_B(), "s", "atlas", 5, None, None, model_facing=False)


SITES = [
    ("normalize_item", normalize_item),
    ("normalize_items", lambda row: normalize_items([row])),
    ("model_item", model_item),
    ("working_context", _working_context_site),
]


@pytest.mark.parametrize(
    "name,carrier,reason", MALFORMED, ids=[m[0] for m in MALFORMED]
)
def test_malformed_carrier_is_a_typed_refusal_at_every_site(
    name, carrier, reason, caplog
):
    row = _base(item_id="test_bad", read_status=carrier)
    expected = (
        "WARNING",
        f"read_status carrier on test_bad is {reason}: refusing to normalize the row without its extraction status",
    )
    for site_name, site in SITES:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(MalformedReadStatusError):
                site(row)
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [expected], (
            site_name
        )


def test_absent_carrier_stays_legacy_and_silent(caplog):
    with caplog.at_level(logging.WARNING):
        assert "read_status" not in normalize_item(_base(read_status=None))
        assert "read_status" not in normalize_item(_base())
    assert caplog.records == []


def test_normalize_items_mixed_pending_and_graph_only():
    out = normalize_items(
        [_base(item_id="test_pending", read_status=STATUS), _base(item_id="test_graph")]
    )
    assert [("read_status" in r) for r in out] == [True, False]


def test_model_facing_item_keeps_carrier_but_not_metadata():
    item = model_item(
        {**normalize_item(_base(read_status=STATUS, metadata={"k": "v"}))}
    )
    assert item["read_status"] == STATUS
    assert item["metadata"] == {}


class _Backend:
    def __init__(self, rows):
        self._rows = rows

    def search(self, query, top_k=5, **kwargs):
        return self._rows


@pytest.mark.parametrize("model_facing", [False, True])
def test_working_context_items_keep_carrier(model_facing):
    rows = [
        normalize_item(_base(item_id="test_pending", read_status=STATUS)),
        normalize_item(_base(item_id="test_graph")),
    ]
    out = _build_working_context(
        _Backend(rows), "s", "atlas", 5, None, None, model_facing=model_facing
    )
    by_id = {i["item_id"]: i for i in out["items"]}
    assert by_id["test_pending"]["read_status"] == STATUS
    assert "read_status" not in by_id["test_graph"]


# -- LocalBackend caller sites over a real lite memory (get / search / list_memories) --

from smartmemory.configuration.switch_testing import switch_override  # noqa: E402
from smartmemory.save.read_port import (  # noqa: E402
    AdmissionRow,
    InMemoryPendingStore,
    LexicalRecord,
    ScopeKeys,
    source_sha256,
)
from smartmemory.search.visibility_gate import SWITCH  # noqa: E402
from smartmemory.tools.factory import create_lite_memory  # noqa: E402

SCOPE = ScopeKeys(None, "local", None)
TEXT = "atlas budget accepted full source text"
TIME = "2026-10-11T12:00:00.000000+00:00"


@pytest.fixture
def local_backend(tmp_path, monkeypatch):
    from smartmemory_app import storage
    from smartmemory_mcp.backends.local import LocalBackend

    mem = create_lite_memory(data_dir=str(tmp_path / "test_u522_mcp"))
    store = InMemoryPendingStore()
    row = AdmissionRow(
        "test_pending", 1, "test_run", TIME, "semantic", "atlas", "atlas budget",
        "received", "available", "indexed", SCOPE, "queued", None,
    )  # fmt: skip
    store.admit(row, LexicalRecord(row.item_id, 1, TEXT, source_sha256(TEXT), SCOPE))
    mem._graph.backend.add_node(
        "test_legacy",
        {
            "content": "atlas budget stored text",
            "origin": "user:test",
            "created_at": TIME,
        },
        memory_type="semantic",
    )
    with switch_override(SWITCH, True):
        mem.bind_pending_reads(store, workspace_hidden=False)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
    try:
        yield LocalBackend()
    finally:
        mem.close()


@pytest.mark.parametrize("on", [False, True])
def test_local_backend_get_search_list_follow_the_gate(local_backend, on):
    with switch_override(SWITCH, on):
        found = {
            r["item_id"]: r
            for r in local_backend.search(
                "atlas budget", top_k=10, reranker="none", enable_hybrid=False
            )
        }
        listed = {
            r["item_id"]: r for r in local_backend.list_memories(limit=20, offset=0)
        }
        legacy = local_backend.get("test_legacy")
        pending = local_backend.get("test_pending")
    assert ("test_pending" in found) is on
    assert ("test_pending" in listed) is on
    assert ("read_status" in legacy) is on
    assert (pending is not None) is on
    if on:
        assert (
            found["test_pending"]["read_status"]["extraction_state"]
            == "extraction_pending"
        )
        assert (
            listed["test_pending"]["read_status"]["extraction_state"]
            == "extraction_pending"
        )
        assert listed["test_legacy"]["read_status"]["extraction_state"] == "unavailable"
        assert pending["read_status"]["extraction_state"] == "extraction_pending"
    else:
        assert all("read_status" not in r for r in (*found.values(), *listed.values()))
