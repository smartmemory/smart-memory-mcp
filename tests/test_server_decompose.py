"""Unit tests for standalone MCP memory_search decompose param."""

from unittest.mock import patch

from tests._tools import tool_fn


class MockBackend:
    def __init__(self, items=None):
        self._items = items or []
        self.last_search_kwargs = {}

    def search(self, query, top_k=5, **kwargs):
        self.last_search_kwargs = {"query": query, "top_k": top_k, **kwargs}
        return self._items


class TestServerDecompose:
    def _call_search(self, **kwargs):
        """Call memory_search and capture kwargs sent to backend.search()."""
        mock_backend = MockBackend()

        # Find the memory_search tool and call its underlying function
        fn = tool_fn("memory_search")

        with patch("smartmemory_mcp.tools.common._backend", mock_backend):
            result = fn(**kwargs)

        return result, mock_backend.last_search_kwargs

    def test_decompose_false_not_in_body(self):
        _, search_kwargs = self._call_search(query="auth")
        assert search_kwargs.get("decompose_query") is False

    def test_decompose_true_in_body(self):
        _, search_kwargs = self._call_search(query="auth", decompose=True)
        assert search_kwargs["decompose_query"] is True

    def test_enable_hybrid_default_true(self):
        """TECHDEBT-SEARCH-1: standalone MCP sends enable_hybrid=True by default."""
        _, search_kwargs = self._call_search(query="auth")
        assert search_kwargs["enable_hybrid"] is True
