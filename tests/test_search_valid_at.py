"""DHRUV-CODING-RULE-P0-1 bug 2: memory_search(valid_at=...) asks "which rule was in force on D".

Four forwarding points, each a place a dropped parameter turns a valid-time question into
an ordinary search with nothing marked: the tool signature, the remote body, the hosted
override, and the result label.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_mcp.backends.models import normalize_item
from smartmemory_mcp.backends.remote import RemoteBackend
from tests._tools import tool_fn
from tests.test_hosted_tools_allowlist import (  # noqa: F401  (autouse fixture)
    _Harness,
    _restore_hosted_mode,
    fake_svc_api,
)


class _Backend:
    def __init__(self):
        self.kwargs: dict[str, Any] = {}

    def search(self, query, top_k=5, **kwargs):
        self.kwargs = kwargs
        return []


def _tool_kwargs(**kwargs):
    backend = _Backend()
    with patch("smartmemory_mcp.tools.common._backend", backend):
        tool_fn("memory_search")(**kwargs)
    return backend.kwargs


def test_tool_forwards_valid_at_beside_as_of_date():
    kwargs = _tool_kwargs(query="x", valid_at="2026-04-10", as_of_date="2026-11-05")
    assert kwargs["valid_at"] == "2026-04-10"
    assert kwargs["as_of_date"] == "2026-11-05"
    assert _tool_kwargs(query="x")["valid_at"] is None


def test_docstring_separates_in_force_from_recorded():
    doc = tool_fn("memory_search").__doc__ or ""
    assert "valid_at" in doc and "in force" in doc
    assert "recorded" in doc


def _remote_body(**kwargs):
    backend = RemoteBackend.__new__(RemoteBackend)
    backend._api_url = "http://test"
    backend._headers = lambda: {}
    captured = {}
    response = MagicMock(status_code=200, headers={})
    response.json.return_value = {"results": []}

    def fake_request(method, url, headers=None, json=None, **kw):
        captured["body"] = json
        return response

    with patch(
        "smartmemory_mcp.backends.remote.httpx.request", side_effect=fake_request
    ):
        backend.search("q", **kwargs)
    return captured["body"]


def test_remote_body_forwards_valid_at_only_when_set():
    assert _remote_body(valid_at="2026-04-10")["valid_at"] == "2026-04-10"
    assert "valid_at" not in _remote_body()


def test_hosted_memory_search_forwards_valid_at(monkeypatch):
    bodies: list[dict] = []
    inner = fake_svc_api()

    def recording(method, url, **kwargs):
        if url.endswith("/memory/search"):
            bodies.append(kwargs.get("json") or {})
        return inner(method, url, **kwargs)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", recording)
    harness = _Harness()

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(
            client,
            session_id,
            "memory_search",
            {"query": "q", "valid_at": "2026-04-10"},
        )

    result = harness.run(body)
    assert "error" not in result, result
    assert bodies and bodies[-1].get("valid_at") == "2026-04-10", bodies


@pytest.mark.parametrize(
    "raw",
    [
        {"item_id": "a", "valid_at_resolution": "created_at_fallback"},
        {"item_id": "a", "metadata": {"valid_at_resolution": "created_at_fallback"}},
    ],
)
def test_valid_at_resolution_label_reaches_the_agent(raw):
    assert normalize_item(raw)["valid_at_resolution"] == "created_at_fallback"
