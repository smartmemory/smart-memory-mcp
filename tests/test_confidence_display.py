"""Contract tests for CORE-PROPS-1 Phase 1b: confidence in MCP search results.

Verifies that memory_search output includes confidence values and ~ markers.
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


class TestConfidenceDisplay:
    def _call_search(self, mock_items, **kwargs):
        """Call memory_search with MockBackend returning mock_items."""
        fn = tool_fn("memory_search")

        mock_backend = MockBackend(mock_items)
        with patch("smartmemory_mcp.tools.common._backend", mock_backend):
            result = fn(query="test", catalog_mode=False, **kwargs)

        return result

    def test_confidence_shown_in_output(self):
        """Search results include [conf: X.XX] when confidence is present."""
        items = [
            {
                "item_id": "abc12345",
                "memory_type": "semantic",
                "content": "Test memory content",
                "confidence": 0.85,
            }
        ]
        result = self._call_search(items)
        assert "[conf: 0.85]" in result

    def test_tilde_marker_on_low_confidence(self):
        """Items with confidence < 0.5 get a ~ prefix."""
        items = [
            {
                "item_id": "def67890",
                "memory_type": "episodic",
                "content": "Low confidence item",
                "confidence": 0.3,
            }
        ]
        result = self._call_search(items)
        assert "~[def67890]" in result
        assert "[conf: 0.30]" in result

    def test_no_tilde_on_high_confidence(self):
        """Items with confidence >= 0.5 do NOT get a ~ prefix."""
        items = [
            {
                "item_id": "ghi11111",
                "memory_type": "semantic",
                "content": "High confidence item",
                "confidence": 0.9,
            }
        ]
        result = self._call_search(items)
        assert "~[" not in result
        assert "[conf: 0.90]" in result

    def test_no_confidence_field_no_marker(self):
        """Items without confidence field show no conf string or marker."""
        items = [
            {
                "item_id": "jkl22222",
                "memory_type": "pending",
                "content": "No confidence field",
            }
        ]
        result = self._call_search(items)
        assert "[conf:" not in result
        assert "~[" not in result
