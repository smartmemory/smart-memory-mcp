"""BUG-MCP-WITHHELD-STATE-1 fix r1: ONE withheld boundary every renderer passes through.

W1 structured text outside metadata, W2 get/explain/decision renderers, W3 a top-level-only marker."""

from __future__ import annotations

import json
from unittest.mock import patch

from smartmemory_mcp.backends.models import normalize_item
from smartmemory_mcp.tools.memory_tools import _build_working_context
from tests._tools import tool_fn

REASON = "legacy_replacement_unproven"
SECRET = "SECRET-ROOT-TEXT"


class _Backend:
    def __init__(self, items=(), get=None, explain=None, decision=None):
        self._items = list(items)
        self._get, self._explain, self._decision = get, explain, decision
        self._last_search_session_id = None

    def search(self, query, top_k=5, **kwargs):
        return self._items

    def get(self, item_id):
        return self._get

    def explain(self, memory_id):
        return self._explain

    def decision_get(self, decision_id, **kwargs):
        return self._decision

    def decision_provenance(self, decision_id, **kwargs):
        return {"decision": self._decision, "evidence": []}

    def decision_history(self, decision_id, **kwargs):
        return {"versions": [self._decision]}


def _leaky(top_level_flag=False):
    item = {
        "item_id": "w-1",
        "content": "",
        "memory_type": "semantic",
        "score": 0.5,
        "as_of_resolution": "resolved",
        "conflict": {"explanation": SECRET, "conflict_type": "x"},
        "derived_from": [SECRET],
        "reference": {"note": SECRET},
        "metadata": {"content_withheld_reason": REASON, "note": SECRET},
    }
    if top_level_flag:
        item["content_withheld"] = True
    else:
        item["metadata"]["content_withheld"] = True
    return item


def _text(out):
    return out if isinstance(out, str) else json.dumps(out, default=str)


def _no_leak(out):
    text = _text(out)
    assert SECRET not in text and REASON not in text and "content_withheld_reason" not in text


def _search(item, **kw):
    with patch("smartmemory_mcp.tools.common._backend", _Backend([item])):
        return tool_fn("memory_search")(query="q", as_of_date="2026-01-01T00:00:00+00:00", **kw)


# W1: the withheld record is a status-only projection, whatever structured text it carried.
def test_w1_structured_text_dropped_in_text_catalog_and_cite():
    for kw in ({"catalog_mode": False}, {"catalog_mode": True}, {"cite": True}):
        out = _search(_leaky(), **kw)
        _no_leak(out)
        assert "unavailable" in _text(out).lower()


# W2: get / explain / decision renderers pass the same boundary.
def test_w2_memory_get_both_modes():
    for include in (False, True):
        with patch("smartmemory_mcp.tools.common._backend", _Backend(get=_leaky())):
            out = tool_fn("memory_get")(item_id="w-1", include_metadata=include)
        _no_leak(out)
        assert "unavailable" in out.lower()


def test_w2_memory_explain_withheld_audit_row_is_status_only():
    row = {
        "content_withheld": True,
        "version": 2,
        "transaction_time_start": "2026-01-01T00:00:00",
        "content_hash": "abc123hash",
        "change_reason": SECRET,
        "content_preview": "",
    }
    result = {"item_id": "w-1", "versions": [row, {"version": 3, "content_preview": "readable"}]}
    with patch("smartmemory_mcp.tools.common._backend", _Backend(explain=result)):
        out = tool_fn("memory_explain")(memory_id="w-1")
    _no_leak(out)
    text = _text(out)
    assert "2026-01-01T00:00:00" not in text and "abc123hash" not in text
    assert "unavailable" in text.lower()
    assert "readable" in text  # the readable row is unchanged


def test_w2_decision_renderers():
    decision = {"decision_id": "d-1", "content": "", "metadata": {"content_withheld": True, "note": SECRET}, "reason": REASON}
    with patch("smartmemory_mcp.tools.common._backend", _Backend(decision=decision)):
        for name, kw in (
            ("decision_get", {"decision_id": "d-1"}),
            ("decision_provenance", {"decision_id": "d-1"}),
            ("decision_history", {"decision_id": "d-1"}),
        ):
            out = tool_fn(name)(**kw)
            _no_leak(out)


# W3: a marker at the item root survives normalizing and the working-context entry.
def test_w3_normalize_item_keeps_root_flag():
    result = normalize_item({"item_id": "w-1", "content": "", "content_withheld": True, "as_of_resolution": "resolved"})
    assert result.get("content_withheld") is True


def test_w3_root_flag_search_says_unavailable():
    item = _leaky(top_level_flag=True)
    item["metadata"] = {}
    out = _search(normalize_item(item), catalog_mode=False)
    assert "unavailable" in out.lower() and "as_of_resolution: resolved" not in out


def test_w3_working_context_entry_keeps_root_flag():
    item = _leaky(top_level_flag=True)
    item["metadata"] = {}
    with patch("smartmemory_mcp.tools.memory_tools.validated_carrier", return_value=None):
        ctx = _build_working_context(_Backend([item]), "s", "q", 5, None, None)
    entries = ctx.get("items") or ctx.get("working_context") or []
    assert entries and entries[0].get("content_withheld") is True
