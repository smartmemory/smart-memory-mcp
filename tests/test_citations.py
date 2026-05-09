"""Tests for RECALL-CITATIONS-1 — MCP memory_search/memory_recall cite=True."""
from __future__ import annotations

from unittest.mock import patch


class MockBackend:
    def __init__(self, items=None):
        self._items = items or []
        self.last_search_kwargs = {}
        self._last_search_session_id = None

    def search(self, query, top_k=5, **kwargs):
        self.last_search_kwargs = {"query": query, "top_k": top_k, **kwargs}
        return self._items


def _mk_item(item_id: str, content: str, score: float = 0.5, mtype: str = "semantic") -> dict:
    return {
        "item_id": item_id,
        "content": content,
        "memory_type": mtype,
        "score": score,
        "metadata": {},
    }


def _get_tool(name: str):
    import smartmemory_mcp.server as srv
    for tool in srv.mcp._tool_manager._tools.values():
        if tool.name == name:
            return tool.fn
    raise AssertionError(f"{name} tool not registered")


class TestMemorySearchCite:
    def test_cite_false_returns_string(self):
        """Default behavior unchanged — memory_search returns the catalog/text string."""
        fn = _get_tool("memory_search")
        backend = MockBackend(items=[_mk_item(f"id-{i}", f"content {i}", score=1.0 - i * 0.1) for i in range(5)])
        with patch("smartmemory_mcp.tools.common._backend", backend):
            out = fn(query="anything", top_k=5)
        assert isinstance(out, str)

    def test_cite_true_returns_structured_payload(self):
        fn = _get_tool("memory_search")
        items = [_mk_item(f"id-{i}", f"content {i}", score=1.0 - i * 0.1) for i in range(5)]
        backend = MockBackend(items=items)
        with patch("smartmemory_mcp.tools.common._backend", backend):
            out = fn(query="anything", top_k=5, cite=True)
        assert isinstance(out, dict)
        assert "items" in out
        assert "citations" in out
        assert "footnote_block" in out

        citations = out["citations"]
        assert len(citations) == 3  # min(3, 5)
        for i, c in enumerate(citations, start=1):
            assert c["n"] == i
            assert c["footnote_marker"] == f"[^{i}]"
            assert c["item_id"]

        # footnote_block parses as markdown footnote lines
        block = out["footnote_block"]
        assert isinstance(block, str)
        assert block.strip()
        for line in block.split("\n"):
            assert line.startswith("[^")
            assert "]: " in line

    def test_cite_true_no_results_distinguishes_states(self):
        """No-results contract: citations: [] is present, never omitted, never raises."""
        fn = _get_tool("memory_search")
        backend = MockBackend(items=[])
        with patch("smartmemory_mcp.tools.common._backend", backend):
            out = fn(query="no-hit", top_k=5, cite=True)
        assert isinstance(out, dict)
        assert out["items"] == []
        assert out["citations"] == []
        assert out["footnote_block"] == ""
