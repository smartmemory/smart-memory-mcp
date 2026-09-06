import pytest
from unittest.mock import Mock, patch
import httpx
from tests._tools import tool_fn
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.backends.local import LocalBackend


def test_tool_hop_forwarding_and_inert_marker():
    backend = Mock()
    backend.search.return_value = []
    search = tool_fn("memory_search")
    with patch("smartmemory_mcp.tools.common._backend", backend):
        search(query="bridge", multi_hop=True, hop_strategy="relevance")
        assert backend.search.call_args.kwargs["hop_strategy"] == "relevance"
        result = search(query="bridge", hop_strategy="consensus")
        assert "inert_parameters" in str(result)
        with pytest.raises(ValueError, match="consensus, relevance, semantic"):
            search(query="bridge", hop_strategy="invalid")


def test_both_backends_forward_strategy(monkeypatch):
    seen = {}

    def request(method, url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(
            200, json={"results": []}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", request)
    remote = RemoteBackend(api_url="http://test", api_key="fake", team_id="ws")
    remote._session["_bootstrapped"] = True
    remote.search("bridge", multi_hop=True, hop_strategy="relevance")
    assert seen["json"]["hop_strategy"] == "relevance"
    local = LocalBackend.__new__(LocalBackend)
    with patch("smartmemory_app.storage.search", return_value=[]) as search:
        local.search("bridge", multi_hop=True, hop_strategy="relevance")
    assert search.call_args.kwargs["hop_strategy"] == "relevance"
