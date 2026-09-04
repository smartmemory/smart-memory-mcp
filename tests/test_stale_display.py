"""Contract tests for CORE-PROPS-1 Phase 2: stale markers in MCP search results.

Verifies that memory_search output includes ⚠ prefix and [STALE] suffix for stale items.
"""

from unittest.mock import patch

from tests._tools import tool_fn


class MockBackend:
    def __init__(self, items=None):
        self._items = items or []
        self.last_search_kwargs = {}

    def search(self, query, top_k=5, **kwargs):
        self.last_search_kwargs = {"query": query, "top_k": top_k, **kwargs}
        return self._items


class TestStaleDisplay:
    def _call_search(self, mock_items, **kwargs):
        """Call memory_search with MockBackend returning mock_items."""
        fn = tool_fn("memory_search")

        mock_backend = MockBackend(mock_items)
        with patch("smartmemory_mcp.tools.common._backend", mock_backend):
            result = fn(query="test", catalog_mode=False, **kwargs)

        return result

    def test_stale_marker_in_output(self):
        """Stale items get ⚠ prefix and [STALE] suffix."""
        items = [
            {
                "item_id": "stale123",
                "memory_type": "semantic",
                "content": "Stale information",
                "confidence": 0.8,
                "stale": True,
            }
        ]
        result = self._call_search(items)
        assert "⚠" in result
        assert "[STALE]" in result
        assert "Stale information" in result

    def test_no_stale_marker_when_false(self):
        """Non-stale items have no ⚠ or [STALE]."""
        items = [
            {
                "item_id": "fresh123",
                "memory_type": "semantic",
                "content": "Fresh information",
                "confidence": 0.9,
                "stale": False,
            }
        ]
        result = self._call_search(items)
        assert "⚠" not in result
        assert "[STALE]" not in result
        assert "Fresh information" in result

    def test_no_stale_field_no_marker(self):
        """Items without stale field have no marker."""
        items = [
            {
                "item_id": "nofld123",
                "memory_type": "pending",
                "content": "No stale field",
            }
        ]
        result = self._call_search(items)
        assert "⚠" not in result
        assert "[STALE]" not in result
