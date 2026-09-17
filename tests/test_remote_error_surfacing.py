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
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"error": "API error 500: boom"}
    )

    with pytest.raises(RuntimeError, match="boom"):
        backend.list_memories()


def test_list_memories_returns_items_on_success(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: {
            "items": [{"item_id": "m-1", "content": "hi", "memory_type": "semantic"}],
            "total": 1,
        },
    )

    out = backend.list_memories()
    assert out["total"] == 1
    assert [i["item_id"] for i in out["items"]] == ["m-1"]


def test_list_memories_preserves_server_total_larger_than_page(monkeypatch) -> None:
    """The backend must not replace the corpus total with the current page size."""
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: {
            "items": [{"item_id": "m-1", "content": "hi", "memory_type": "semantic"}],
            "total": 4000,
            "limit": 1,
            "offset": 0,
        },
    )

    out = backend.list_memories(limit=1)

    assert out["total"] == 4000
    assert len(out["items"]) == 1


def test_stats_raises_on_error_dict(monkeypatch) -> None:
    """A failed stats call must surface as failure, never as a zero count."""
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"error": "API error 500: boom"}
    )

    with pytest.raises(RuntimeError, match="boom"):
        backend.stats()


def test_search_by_metadata_raises_on_error_dict(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"error": "API error 403: nope"}
    )

    with pytest.raises(RuntimeError, match="nope"):
        backend.search_by_metadata("k", "v")


def test_search_by_metadata_returns_items_on_success(monkeypatch) -> None:
    """The service returns the {items, count} envelope — NOT a bare item.

    The previous version of this test fed a bare item dict, a shape
    `GET /memory/by-metadata` has never returned (crud.py returns
    `{"items": [...], "count": N}`). It therefore stayed green while the backend
    wrapped the envelope itself via `normalize_items([result])`, which — because
    `normalize_item` is all `.get()`-with-defaults — emitted one BLANK memory and
    dropped every real hit without raising.
    """
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: {
            "items": [
                {"item_id": "m-2", "content": "meta hit", "memory_type": "semantic"},
                {"item_id": "m-3", "content": "second hit", "memory_type": "semantic"},
            ],
            "count": 2,
        },
    )

    out = backend.search_by_metadata("k", "v")
    assert [i["item_id"] for i in out] == ["m-2", "m-3"]
    assert [i["content"] for i in out] == ["meta hit", "second hit"]


def test_search_by_metadata_empty_envelope_is_no_results(monkeypatch) -> None:
    """`{"items": [], "count": 0}` must be zero results, not one blank memory."""
    backend = _backend()
    monkeypatch.setattr(backend, "_request", lambda *a, **k: {"items": [], "count": 0})

    assert backend.search_by_metadata("k", "v") == []


def test_search_by_metadata_forwards_top_k_as_limit(monkeypatch) -> None:
    """top_k was dropped, pinning every call to the service default of 25."""
    seen: dict[str, str] = {}

    backend = _backend()

    def _capture(method, path, params=None, **k):
        seen.update(params or {})
        return {"items": [], "count": 0}

    monkeypatch.setattr(backend, "_request", _capture)

    backend.search_by_metadata("k", "v", top_k=50)
    assert seen["limit"] == "50"

    backend.search_by_metadata("k", "v", top_k=9999)
    assert seen["limit"] == "200"  # service caps at 200


def test_search_by_metadata_legacy_bare_array_still_works(monkeypatch) -> None:
    """Pre-GRAPH-API-1l services returned a bare top-level array."""
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: [
            {"item_id": "m-9", "content": "legacy", "memory_type": "semantic"}
        ],
    )

    assert [i["item_id"] for i in backend.search_by_metadata("k", "v")] == ["m-9"]


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
            200,
            json=envelope,
            request=httpx.Request("POST", "https://api.test/memory/search"),
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
            200,
            json={"results": "not-a-list"},
            request=httpx.Request("POST", "https://api.test/memory/search"),
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
            response=httpx.Response(
                500, request=httpx.Request("POST", "https://api.test/memory/search")
            ),
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", _raise)

    with pytest.raises(RuntimeError, match="API error 500"):
        backend.search("anything")


# --- Codex review 2026-08-02: error paths, not just envelope shapes ----------
# The original sweep verified envelope UNPACKING across all call sites and
# declared the file clean. It never audited the ERROR paths, where add() and
# get() were both reporting failures as success.


def test_add_raises_instead_of_returning_the_error_dict_as_an_item_id(
    monkeypatch,
) -> None:
    """add() used to hand back "{'error': 'API error 500: ...'}" AS THE NEW ITEM ID."""
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"error": "API error 500: boom"}
    )

    with pytest.raises(RuntimeError, match="boom"):
        backend.add("some content")


def test_add_raises_when_no_item_id_comes_back(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(backend, "_request", lambda *a, **k: {"status": "created"})

    with pytest.raises(RuntimeError, match="no item id"):
        backend.add("some content")


def test_add_returns_id_from_the_id_key(monkeypatch) -> None:
    """The service returns {"id": ...}, not {"item_id": ...} (crud.py add_memory)."""
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"id": "m-77", "status": "created"}
    )

    assert backend.add("some content") == "m-77"


def test_get_raises_on_outage_instead_of_reporting_absence(monkeypatch) -> None:
    """A 403/500 rendered as "Memory item not found." is silent degradation."""
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"error": "API error 500: boom"}
    )

    with pytest.raises(RuntimeError, match="boom"):
        backend.get("m-1")


def test_get_still_returns_none_for_genuine_absence(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"error": "API error 404: not found"}
    )
    assert backend.get("m-1") is None

    monkeypatch.setattr(backend, "_request", lambda *a, **k: None)
    assert backend.get("m-1") is None


def test_list_memories_rejects_a_non_list_items_value(monkeypatch) -> None:
    """A malformed page must surface as failure, not as an empty corpus."""
    backend = _backend()
    monkeypatch.setattr(
        backend, "_request", lambda *a, **k: {"items": {"a": 1, "b": 2}, "total": 2}
    )

    with pytest.raises(RuntimeError, match="items.*list"):
        backend.list_memories()


def test_superseded_fields_survive_normalization(monkeypatch) -> None:
    """crud.py returns superseded/superseded_by; dropping them hid obsolete memories."""
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_request",
        lambda *a, **k: {
            "items": [
                {
                    "item_id": "old",
                    "content": "stale fact",
                    "superseded": True,
                    "superseded_by": "new",
                },
                {"item_id": "cur", "content": "current fact"},
            ],
            "count": 2,
        },
    )

    out = backend.search_by_metadata("k", "v")
    assert out[0]["superseded"] is True
    assert out[0]["superseded_by"] == "new"
    assert out[1]["superseded"] is False
