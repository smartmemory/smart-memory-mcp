"""Regression coverage for the hosted memory update HTTP method."""

from __future__ import annotations

import httpx

from smartmemory_mcp.backends.remote import RemoteBackend


def test_update_uses_patch_for_memory_route(monkeypatch) -> None:
    backend = RemoteBackend(
        api_url="https://api.test", api_key="sk_test", team_id="ws-1"
    )
    backend._session["_bootstrapped"] = True
    captured: dict[str, str] = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["path"] = httpx.URL(url).path
        return httpx.Response(
            200,
            json={"updated": True},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_request)

    assert backend.update("memory-123", content="updated") == {"updated": True}
    assert captured == {"method": "PATCH", "path": "/memory/memory-123"}
