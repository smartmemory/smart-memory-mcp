"""BUG-MCP-WITHHELD-TYPED-KEEP-1: the withheld projection keeps an allowlisted field only when its value has the field's type."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from smartmemory_mcp.tools.common import withheld_view
from tests._tools import tool_fn

SECRET = "SECRET-TYPED-TEXT"


def _flagged(**over):
    item = {
        "item_id": "w-1",
        "memory_type": "semantic",
        "score": 0.5,
        "matched_by": ["lexical", "semantic"],
        "version": 2,
        "content": "",
        "metadata": {"content_withheld": True},
    }
    item.update(over)
    return item


def _dump(x):
    return json.dumps(x, default=str)


# R5 (fix r1): one case per field/value pair, so each pair fails or passes on its own.
@pytest.mark.parametrize(
    "bad", [[SECRET], [{"note": SECRET}], ["lexical", SECRET], ["not-a-lane"], SECRET, {"a": SECRET}, 7], ids=repr
)
def test_n1_matched_by_values_dropped(bad):
    out = withheld_view(_flagged(matched_by=bad))
    assert "matched_by" not in out, bad
    assert SECRET not in _dump(out)


@pytest.mark.parametrize("bad", [SECRET, {"note": SECRET}, ["semantic"], 5], ids=repr)
def test_n2_memory_type_dropped(bad):
    assert "memory_type" not in withheld_view(_flagged(memory_type=bad)), bad


@pytest.mark.parametrize("field", ["item_id", "decision_id"])
@pytest.mark.parametrize("bad", [{"note": SECRET}, [SECRET], 5], ids=repr)
def test_n2_ids_dropped(field, bad):
    assert field not in withheld_view(_flagged(**{field: bad})), bad


def test_n2_str_decision_id_kept():
    assert withheld_view(_flagged(decision_id="d-1"))["decision_id"] == "d-1"


@pytest.mark.parametrize("bad", ["0.5", True, float("nan"), float("inf"), {"n": SECRET}, None], ids=repr)
def test_n3_score_dropped(bad):
    assert "score" not in withheld_view(_flagged(score=bad)), bad


@pytest.mark.parametrize("bad", ["2", True, 1.5, {"n": SECRET}], ids=repr)
def test_n3_version_dropped(bad):
    assert "version" not in withheld_view(_flagged(version=bad)), bad


def test_n4_negative_control_clean_values_kept():
    out = withheld_view(_flagged(decision_id="d-1"))
    assert out["item_id"] == "w-1" and out["decision_id"] == "d-1"
    assert out["memory_type"] == "semantic" and out["score"] == 0.5
    assert out["matched_by"] == ["lexical", "semantic"] and out["version"] == 2
    assert out["content_withheld"] is True and out["as_of_resolution"] == "unavailable"


class _Backend:
    def __init__(self, items):
        self._items = items
        self._last_search_session_id = None

    def search(self, query, top_k=5, **kwargs):
        return self._items


def _search(item, **kw):
    with patch("smartmemory_mcp.tools.common._backend", _Backend([item])):
        return tool_fn("memory_search")(query="q", as_of_date="2026-01-01T00:00:00+00:00", **kw)


def test_n5_end_to_end_text_mode():
    out = _search(_flagged(matched_by=[SECRET]), catalog_mode=False)
    assert SECRET not in out and "found by" not in out and "unavailable" in out.lower()
    clean = _search(_flagged(), catalog_mode=False)
    assert "found by: lexical, semantic" in clean and "unavailable" in clean.lower()


def test_n6_readable_record_unchanged():
    item = {"item_id": {"x": SECRET}, "memory_type": SECRET, "score": "hi", "matched_by": [SECRET], "content": "text"}
    assert withheld_view(item) == item
