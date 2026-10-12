"""U5.22 local MCP normalizers carry the merged-read extraction status. # trace G:G43,G46,G114,G136,G142

The carrier (`read_status`) exists only on the visibility-gate-ON arm. Every
normalizer must keep it when present and add nothing when absent, so the legacy
arm's output is unchanged.
"""

import pytest

from smartmemory_mcp.backends.models import normalize_item, normalize_items
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


def test_normalize_item_ignores_a_non_dict_carrier():
    assert "read_status" not in normalize_item(_base(read_status="extraction_pending"))


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
