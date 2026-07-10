"""RemoteBackend must surface API errors, not mask them.

Covers the CODE_REVIEW_2026-07-02 findings:
- list_memories mapped an {"error": ...} response to [] ("No memories found"),
  hiding a backend 500 as an empty workspace (silent degradation).
- search_by_metadata returned [error_dict] as if it were a memory item, which the
  tool layer then iterated / KeyErrored on.

Both must now raise so @graceful renders a clean tool error.
"""

from __future__ import annotations

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend


def _backend() -> RemoteBackend:
    return RemoteBackend(api_url="https://api.test", api_key="sk_test", team_id="ws-1")


def test_list_memories_raises_on_error_dict(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(backend, "_request", lambda *a, **k: {"error": "API error 500: boom"})

    with pytest.raises(RuntimeError, match="boom"):
        backend.list_memories()


def test_list_memories_returns_items_on_success(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: {"items": [{"item_id": "m-1", "content": "hi", "memory_type": "semantic"}], "total": 1},
    )

    out = backend.list_memories()
    assert [i["item_id"] for i in out] == ["m-1"]


def test_search_by_metadata_raises_on_error_dict(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(backend, "_request", lambda *a, **k: {"error": "API error 403: nope"})

    with pytest.raises(RuntimeError, match="nope"):
        backend.search_by_metadata("k", "v")


def test_search_by_metadata_returns_item_on_success(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: {"item_id": "m-2", "content": "meta hit", "memory_type": "semantic"},
    )

    out = backend.search_by_metadata("k", "v")
    assert [i["item_id"] for i in out] == ["m-2"]


def test_search_unwraps_lineage_response_envelope(monkeypatch) -> None:
    """CORE-RECALL-LINEAGE-1: the service returns {"results": [...], "group_roots": {...}}.

    search() must unwrap the envelope — treating the dict as "not a list" silently
    rendered every remote memory_search as "No results" (found live in
    DEMO-WALKTHROUGH-4 spike 0.5/0.10 against the local full stack).
    """
    import httpx

    backend = _backend()
    envelope = {
        "results": [{"item_id": "m-3", "content": "hit", "memory_type": "episodic"}],
        "group_roots": {},
    }

    def _ok(*a, **k):
        return httpx.Response(
            200, json=envelope, request=httpx.Request("POST", "https://api.test/memory/search")
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", _ok)

    out = backend.search("anything")
    assert [i["item_id"] for i in out] == ["m-3"]


def test_search_returns_empty_on_malformed_envelope(monkeypatch) -> None:
    """A dict response without a list under "results" degrades to [] (not a crash)."""
    import httpx

    backend = _backend()

    def _ok(*a, **k):
        return httpx.Response(
            200, json={"results": "not-a-list"}, request=httpx.Request("POST", "https://api.test/memory/search")
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", _ok)
    assert backend.search("anything") == []


def test_search_raises_on_error_dict(monkeypatch) -> None:
    """search() must also surface errors rather than returning [error_dict]
    (which KeyErrors in the catalog formatter)."""
    backend = _backend()
    import httpx

    def _raise(*a, **k):
        raise httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "https://api.test/memory/search"),
            response=httpx.Response(500, request=httpx.Request("POST", "https://api.test/memory/search")),
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", _raise)

    with pytest.raises(RuntimeError, match="API error 500"):
        backend.search("anything")
