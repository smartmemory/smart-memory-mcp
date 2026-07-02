"""Unit tests for RLM-1c multi-hop params in MCP memory_search tool."""

from unittest.mock import patch


class MockBackend:
    def __init__(self, items=None):
        self._items = items or []
        self.last_search_kwargs = {}

    def search(self, query, top_k=5, **kwargs):
        self.last_search_kwargs = {"query": query, "top_k": top_k, **kwargs}
        return self._items


class TestServerMultiHop:
    def _call_search(self, **kwargs):
        """Call memory_search and capture kwargs sent to backend.search()."""
        import smartmemory_mcp.server as srv

        mock_backend = MockBackend()

        fn = None
        for tool in srv.mcp._tool_manager._tools.values():
            if tool.name == "memory_search":
                fn = tool.fn
                break

        assert fn is not None, "memory_search tool not found"

        with patch("smartmemory_mcp.tools.common._backend", mock_backend):
            result = fn(**kwargs)

        return result, mock_backend.last_search_kwargs

    def test_multi_hop_false_by_default(self):
        _, search_kwargs = self._call_search(query="auth")
        assert search_kwargs.get("multi_hop") is False

    def test_multi_hop_true_forwarded(self):
        _, search_kwargs = self._call_search(
            query="auth", multi_hop=True, max_hops=2, budget_ms=500
        )
        assert search_kwargs["multi_hop"] is True
        assert search_kwargs["max_hops"] == 2
        assert search_kwargs["budget_ms"] == 500

    def test_multi_hop_defaults_forwarded(self):
        _, search_kwargs = self._call_search(query="auth", multi_hop=True)
        assert search_kwargs["multi_hop"] is True
        assert search_kwargs["max_hops"] == 3
        assert search_kwargs["budget_ms"] == 1500


class _SearchResponse:
    """Minimal httpx.Response stand-in for RemoteBackend.search()."""

    status_code = 200
    headers: dict = {}

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return []


class TestRemoteBackendMultiHop:
    """Test that RemoteBackend.search() serializes multi-hop params.

    search() posts via ``httpx.request`` directly (not ``self._request``), so we
    patch that and capture the JSON body actually sent to the API.
    """

    def _backend_capturing(self, monkeypatch) -> tuple:
        import smartmemory_mcp.backends.remote as remote_mod
        from smartmemory_mcp.backends.remote import RemoteBackend

        backend = RemoteBackend(api_url="http://test:9001", api_key="sk_test", team_id="ws-1")
        # Skip the /auth/me bootstrap network call in _headers().
        backend._session["_bootstrapped"] = True

        captured_body: dict = {}

        def mock_request(method, url, **kwargs):
            captured_body.update(kwargs.get("json", {}))
            return _SearchResponse()

        monkeypatch.setattr(remote_mod.httpx, "request", mock_request)
        return backend, captured_body

    def test_remote_search_forwards_multi_hop(self, monkeypatch):
        backend, captured_body = self._backend_capturing(monkeypatch)

        backend.search("auth", top_k=5, multi_hop=True, max_hops=2, budget_ms=800)

        assert captured_body.get("multi_hop") is True
        assert captured_body.get("max_hops") == 2
        assert captured_body.get("budget_ms") == 800

    def test_remote_search_omits_multi_hop_when_false(self, monkeypatch):
        backend, captured_body = self._backend_capturing(monkeypatch)

        backend.search("auth", top_k=5)

        assert "multi_hop" not in captured_body
