"""RemoteBackend.search forwards channel_weights (CORE-SEARCH-2a)."""

from __future__ import annotations

import json

import httpx

from smartmemory_mcp.backends.remote import RemoteBackend


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def fake_request(method, url, **kwargs):
        captured["body"] = kwargs.get("json")
        request = httpx.Request(method, url)
        return httpx.Response(200, json={"results": []}, request=request)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_request)
    return captured


def test_channel_weights_are_forwarded(monkeypatch) -> None:
    captured = _capture(monkeypatch)
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True

    backend.search("q", channel_weights={"vector": 0.7, "keyword": 0.3})

    assert captured["body"]["channel_weights"] == {"vector": 0.7, "keyword": 0.3}
    assert json.dumps(captured["body"])  # body stays JSON-serialisable


def test_channel_weights_omitted_when_absent(monkeypatch) -> None:
    captured = _capture(monkeypatch)
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True

    backend.search("q")

    assert "channel_weights" not in captured["body"]
