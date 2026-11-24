"""BUG-MCP-WITHHELD-TYPED-KEEP-1 fix r1: a withheld record is projected before any of its values is read, and a
renderer never needs a field the projection may drop.

R1 get_working_context projects withheld rows, R2 a withheld score never reaches a recall error, R3 a dropped id or
type does not abort a read, R4 a large finite integer score neither raises nor reaches float formatting."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from smartmemory_mcp.tools import memory_tools
from smartmemory_mcp.tools.common import WITHHELD_TEXT, withheld_view
from tests._tools import tool_fn

SECRET = "REVIEW-MARKER-SECRET"


@pytest.fixture(autouse=True)
def _quiet_deprecation():
    memory_tools._RECALL_DEPRECATION_WARNED = True
    yield
    memory_tools._RECALL_DEPRECATION_WARNED = False


class _Backend:
    """search/get only: no ``recall``, so memory_recall takes the working-context route."""

    def __init__(self, items=(), get=None):
        self._items = list(items)
        self._get = get
        self._last_search_session_id = None

    def search(self, query, top_k=5, **kwargs):
        return list(self._items)

    def get(self, item_id):
        return self._get


def _flagged(at="metadata", **over):
    item = {"item_id": "w-1", "memory_type": "pending", "score": 0.5, "content": "", "metadata": {}}
    if at == "metadata":
        item["metadata"]["content_withheld"] = True
    else:
        item["content_withheld"] = True
    item.update(over)
    return item


def _readable(**over):
    item = {"item_id": "r-1", "memory_type": "pending", "score": 0.4, "content": "the readable sibling", "metadata": {}}
    item.update(over)
    return item


def _dump(x):
    return x if isinstance(x, str) else json.dumps(x, default=str)


def _call(name, backend, **kw):
    with patch("smartmemory_mcp.tools.common._backend", backend):
        return tool_fn(name)(**kw)


def _search(items, **kw):
    return _call("memory_search", _Backend(items), query="q", as_of_date="2026-01-01T00:00:00+00:00", **kw)


# R1 (HIGH 1): the public working-context response is the unavailable projection, for both marker locations.
@pytest.mark.parametrize("at", ["metadata", "root"])
def test_r1_get_working_context_projects_withheld(at):
    row = _flagged(at=at, item_id={"note": SECRET}, via_supersession_of=SECRET)
    out = _call("get_working_context", _Backend([row, _readable()]), session_id="s", query="q")
    assert SECRET not in _dump(out)
    withheld, readable = out["items"]
    assert withheld["content_withheld"] is True
    assert withheld["as_of_resolution"] == "unavailable"
    assert withheld["content"] == WITHHELD_TEXT
    assert "item_id" not in withheld
    assert readable["item_id"] == "r-1" and readable["content"] == "the readable sibling"


# R2 (HIGH 2): a withheld score is checked before it is converted; recall returns the unavailable item without it.
@pytest.mark.parametrize("at", ["metadata", "root"])
def test_r2_recall_withheld_text_score_never_raises(at):
    backend = _Backend([_flagged(at=at, score=SECRET)])
    text = _call("memory_recall", backend, query="q")
    assert SECRET not in text and "unavailable" in text.lower()
    cited = _call("memory_recall", backend, query="q", cite=True)
    assert SECRET not in _dump(cited)
    assert cited["items"][0]["content_withheld"] is True and "score" not in cited["items"][0]


def test_r2_malformed_carrier_error_never_names_withheld_id():
    row = _flagged(item_id={"note": SECRET}, read_status="not-a-dict")
    try:
        out = _call("get_working_context", _Backend([row]), session_id="s", query="q")
    except Exception as exc:  # the carrier refusal is kept; only its text is checked
        out = f"{type(exc).__name__}: {exc}"
    assert SECRET not in _dump(out)


# R3 (MEDIUM): a dropped id or type is omitted from the display; the unavailable text and the readable sibling stay.
_DROPPED = [
    pytest.param({"item_id": {"note": SECRET}}, id="dict-id"),
    pytest.param({"memory_type": "deployment_custom_type"}, id="deployment-type"),
]


@pytest.mark.parametrize("over", _DROPPED)
@pytest.mark.parametrize("mode", [{"catalog_mode": False}, {"catalog_mode": True}])
def test_r3_search_page_survives_dropped_field(over, mode):
    out = _search([_flagged(**over), _readable()], **mode)
    assert WITHHELD_TEXT in out and "the readable sibling" in out and "r-1" in out
    assert SECRET not in out and "None" not in out


def test_r3_recall_page_survives_dropped_id():
    out = _call("memory_recall", _Backend([_flagged(item_id={"note": SECRET}), _readable()]), query="q")
    assert "unavailable" in out.lower() and "the readable sibling" in out and "IDs: r-1)" in out
    assert SECRET not in out


@pytest.mark.parametrize("include", [False, True])
def test_r3_memory_get_survives_dropped_type(include):
    item = _flagged(memory_type="deployment_custom_type")
    out = _call("memory_get", _Backend(get=item), item_id="w-1", include_metadata=include)
    assert WITHHELD_TEXT in out and "Type:" not in out and "deployment_custom_type" not in out


# R4 (LOW 1): a finite integer beyond float range is dropped without raising; an ordinary integer is kept and renders.
def test_r4_huge_int_score_dropped_without_raising():
    out = withheld_view(_flagged(score=10**309))
    assert "score" not in out and out["content_withheld"] is True


def test_r4_int_score_in_float_range_kept():
    assert withheld_view(_flagged(score=10**6))["score"] == 10**6


@pytest.mark.parametrize("score", [10**309, 10**6], ids=["int-1e309", "int-1e6"])
@pytest.mark.parametrize("mode", [{"catalog_mode": False}, {"catalog_mode": True}, {"cite": True}])
def test_r4_large_int_score_renders(score, mode):
    out = _search([_flagged(score=score), _readable()], **mode)
    assert "unavailable" in _dump(out).lower() and "the readable sibling" in _dump(out)
