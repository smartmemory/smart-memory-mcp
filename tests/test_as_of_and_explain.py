"""PLAT-AUDITABLE-MEMORY-1 MCP surface: as-of search params + memory_explain."""

from unittest.mock import MagicMock, patch


class MockBackend:
    def __init__(self):
        self.last_search_kwargs = {}
        self.explain_result = {"item": {"id": "x"}, "chain_verified": True}

    def search(self, query, top_k=5, **kwargs):
        self.last_search_kwargs = {"query": query, "top_k": top_k, **kwargs}
        return []

    def explain(self, memory_id, **kwargs):
        if memory_id == "missing":
            return None
        return self.explain_result


def _tool(name):
    import smartmemory_mcp.server as srv

    for tool in srv.mcp._tool_manager._tools.values():
        if tool.name == name:
            return tool.fn
    raise AssertionError(f"{name} tool not found")


class TestSearchAsOfParams:
    def _call(self, **kwargs):
        backend = MockBackend()
        with patch("smartmemory_mcp.tools.common._backend", backend):
            _tool("memory_search")(**kwargs)
        return backend.last_search_kwargs

    def test_defaults_off(self):
        kwargs = self._call(query="x")
        assert kwargs["as_of_date"] is None
        assert kwargs["include_superseded"] is False
        assert kwargs["include_retracted"] is False
        assert kwargs["include_archived"] is False  # CORE-ARCHIVED-RECALL-1

    def test_forwarded_when_set(self):
        kwargs = self._call(
            query="x", as_of_date="2026-01-01T00:00:00+00:00", include_superseded=True
        )
        assert kwargs["as_of_date"] == "2026-01-01T00:00:00+00:00"
        assert kwargs["include_superseded"] is True

    def test_lifecycle_flags_forwarded_when_set(self):
        """CORE-ARCHIVED-RECALL-1 — third forwarding point for the third flag.

        The tool signature, the backend call, and the remote body allowlist are
        three separate places a flag has to be named; blueprint correction C3
        called a missed one a silent param drop, and this one is silent in the
        worse direction: the default now HIDES, so dropping the flag returns a
        filtered view to a caller who explicitly asked for everything.
        """
        kwargs = self._call(query="x", include_retracted=True, include_archived=True)
        assert kwargs["include_retracted"] is True
        assert kwargs["include_archived"] is True


class TestMemoryExplainTool:
    def test_explain_returns_contract_shape(self):
        backend = MockBackend()
        with patch("smartmemory_mcp.tools.common._backend", backend):
            result = _tool("memory_explain")("some-id")
        assert result["chain_verified"] is True

    def test_explain_absent_item(self):
        backend = MockBackend()
        with patch("smartmemory_mcp.tools.common._backend", backend):
            result = _tool("memory_explain")("missing")
        assert "not found" in result


class TestRemoteBackendForwarding:
    def _search_body(self, **kwargs):
        from smartmemory_mcp.backends.remote import RemoteBackend

        backend = RemoteBackend.__new__(RemoteBackend)
        backend._api_url = "http://test"
        backend._headers = lambda: {}
        captured = {}

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"results": []}
        response.headers = {}

        def fake_request(method, url, headers=None, json=None, **kw):
            captured["body"] = json
            return response

        with patch(
            "smartmemory_mcp.backends.remote.httpx.request", side_effect=fake_request
        ):
            backend.search("q", **kwargs)
        return captured["body"]

    def test_remote_body_forwards_as_of_params(self):
        body = self._search_body(
            as_of_date="2026-01-01T00:00:00+00:00", include_superseded=True
        )
        assert body["as_of_date"] == "2026-01-01T00:00:00+00:00"
        assert body["include_superseded"] is True

    def test_remote_body_forwards_include_archived(self):
        body = self._search_body(include_archived=True)
        assert body["include_archived"] is True

    def test_remote_body_omits_when_unset(self):
        body = self._search_body()
        assert "as_of_date" not in body
        assert "include_superseded" not in body
        assert "include_retracted" not in body
        assert "include_archived" not in body

    def test_remote_explain_hits_explain_route(self):
        from smartmemory_mcp.backends.remote import RemoteBackend

        backend = RemoteBackend.__new__(RemoteBackend)
        calls = {}

        def fake_request(method, path, **kw):
            calls["method"] = method
            calls["path"] = path
            return {"item": {"id": "m1"}}

        backend._request = fake_request
        backend._fmt_error = lambda r: None
        result = backend.explain("m1")
        assert calls == {"method": "GET", "path": "/memory/m1/explain"}
        assert result["item"]["id"] == "m1"


class TestLocalBackendExplain:
    def test_local_delegates_to_core_facade(self):
        from smartmemory_mcp.backends.local import LocalBackend

        backend = LocalBackend.__new__(LocalBackend)
        backend._mem = MagicMock()
        backend._mem.explain.return_value = {"item": {"id": "m1"}}
        assert backend.explain("m1")["item"]["id"] == "m1"
        backend._mem.explain.assert_called_once_with("m1")
