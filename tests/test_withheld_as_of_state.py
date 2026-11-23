"""BUG-MCP-WITHHELD-STATE-1: a withheld as-of version reads as unavailable, never as a blank resolved hit."""

from __future__ import annotations

import json
from unittest.mock import patch

from tests._tools import tool_fn

REASON = "legacy_replacement_unproven"
FREE_TEXT = "SECRET-META-TEXT"


class _Backend:
    def __init__(self, items):
        self._items = items
        self._last_search_session_id = None

    def search(self, query, top_k=5, **kwargs):
        return self._items


def _withheld():
    return {
        "item_id": "w-1",
        "content": "",
        "memory_type": "semantic",
        "score": 0.5,
        "as_of_resolution": "resolved",
        "metadata": {
            "content_withheld": True,
            "content_withheld_reason": REASON,
            "note": FREE_TEXT,
        },
    }


def _readable():
    return {
        "item_id": "r-1",
        "content": "the readable old answer",
        "memory_type": "semantic",
        "score": 0.5,
        "as_of_resolution": "resolved",
        "metadata": {},
    }


def _search(item, **kw):
    fn = tool_fn("memory_search")
    with patch("smartmemory_mcp.tools.common._backend", _Backend([item])):
        return fn(query="q", as_of_date="2026-01-01T00:00:00+00:00", **kw)


def _no_leak(out):
    text = json.dumps(out) if not isinstance(out, str) else out
    assert REASON not in text and FREE_TEXT not in text and "content_withheld_reason" not in text


def test_text_mode_says_unavailable():  # N1, N4
    out = _search(_withheld(), catalog_mode=False)
    assert "unavailable" in out.lower()
    assert "as_of_resolution: resolved" not in out
    _no_leak(out)


def test_catalog_mode_says_unavailable():  # N2, N4
    out = _search(_withheld(), catalog_mode=True)
    assert "unavailable" in out.lower()
    assert "as_of_resolution: resolved" not in out
    _no_leak(out)


def test_cite_payload_says_unavailable():  # N3, N4
    out = _search(_withheld(), cite=True)
    item = out["items"][0]
    assert item["content_withheld"] is True
    assert item["as_of_resolution"] == "unavailable"
    assert "unavailable" in out["citations"][0]["preview"].lower()
    assert "unavailable" in out["footnote_block"].lower()
    _no_leak(out)


def test_readable_hit_unchanged():  # N5
    for kw in ({"catalog_mode": False}, {"catalog_mode": True}):
        out = _search(_readable(), **kw)
        assert "the readable old answer" in out
        assert "as_of_resolution: resolved" in out
        assert "unavailable" not in out.lower()
    out = _search(_readable(), cite=True)
    assert "content_withheld" not in out["items"][0]
    assert out["items"][0]["as_of_resolution"] == "resolved"
    assert "the readable old answer" in out["citations"][0]["preview"]
