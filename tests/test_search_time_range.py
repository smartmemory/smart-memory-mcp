from unittest.mock import Mock, patch
from smartmemory_mcp.tools.search_window import with_search_window
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
import httpx


def test_echo_all_output_shapes():
    for payload in ("No results", {"items": [], "citations": []}):
        seen = {}

        @with_search_window
        def tool(since=None, until=None):
            seen.update(since=since, until=until)
            return payload

        result = tool(since="7d", until="24h")
        assert seen["since"] < seen["until"]
        assert "window" in str(result) and seen["since"] in str(result)


def test_remote_bound_forwarding(monkeypatch):
    seen = {}

    def request(method, url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(
            200, json={"results": []}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", request)
    backend = RemoteBackend(api_url="http://test", api_key="fake", team_id="ws")
    backend._session["_bootstrapped"] = True
    backend.search("atlas", since="2026-09-01")
    assert seen["json"]["since"] == "2026-09-01"
    with patch.object(backend, "_request", return_value={"items": []}) as req:
        backend.search_by_metadata("project", "atlas", until="2026-09-03")
        assert req.call_args.kwargs["params"]["until"] == "2026-09-03"


def test_local_metadata_range_forwarding():
    backend = LocalBackend.__new__(LocalBackend)
    memory = Mock()
    memory.search_by_metadata.return_value = []
    backend._mem = memory
    backend.search_by_metadata("project", "atlas", since="2026-09-01")
    assert memory.search_by_metadata.call_args.kwargs["since"] == "2026-09-01"
