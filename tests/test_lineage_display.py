"""Contract tests for CORE-PROPS-1 Phase 4: derived_from in MCP search results."""

from unittest.mock import patch

from tests._tools import tool_fn


class MockBackend:
    def __init__(self, items=None):
        self._items = items or []
        self.last_search_kwargs = {}

    def search(self, query, top_k=5, **kwargs):
        self.last_search_kwargs = {"query": query, "top_k": top_k, **kwargs}
        return self._items


class TestLineageDisplay:
    def _call_search(self, mock_items):
        fn = tool_fn("memory_search")

        mock_backend = MockBackend(mock_items)
        with patch("smartmemory_mcp.tools.common._backend", mock_backend):
            return fn(query="test", catalog_mode=False)

    def test_derived_from_shown(self):
        items = [
            {
                "item_id": "derived-1",
                "memory_type": "semantic",
                "content": "Derived item",
                "confidence": 0.8,
                "derived_from": "source-abc12345",
            }
        ]
        result = self._call_search(items)
        assert "[→source-a]" in result

    def test_no_derived_from_no_marker(self):
        items = [
            {
                "item_id": "root-1",
                "memory_type": "episodic",
                "content": "Root item",
                "confidence": 0.9,
            }
        ]
        result = self._call_search(items)
        assert "[→" not in result
