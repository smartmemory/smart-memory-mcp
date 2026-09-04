"""GRAPH-API-1l: metadata filters must reach the service, and local mode must work.

Two defect families are covered here:

1. `RemoteBackend.list_memories` accepted arbitrary **kwargs but forwarded only
   limit/offset, so a caller passing metadata_key/metadata_value got a silently
   UNFILTERED list — wrong results, no error.

2. `LocalBackend.list_memories`, `.search_by_metadata` and `.clear_user_memories`
   delegated to methods that do not exist on `smartmemory.SmartMemory`
   (verified against the core facade 2026-08-02), so all three raised
   AttributeError on every local-mode call.
"""

from __future__ import annotations

import pytest

from smartmemory_mcp.backends.local import LocalBackend, _metadata_matches
from smartmemory_mcp.backends.remote import RemoteBackend


def _remote() -> RemoteBackend:
    return RemoteBackend(api_url="https://api.test", api_key="sk_test", team_id="ws-1")


def _local(hits: list) -> LocalBackend:
    """LocalBackend with a stub core object — __init__ requires the smartmemory package."""

    class _FakeMem:
        def __init__(self) -> None:
            self.cleared = False
            self.search_calls: list[tuple] = []

        def search(self, query, top_k=5, **kw):
            self.search_calls.append((query, top_k))
            return hits

        def clear(self):
            self.cleared = True

    backend = object.__new__(LocalBackend)
    backend._mem = _FakeMem()
    return backend


# --- Remote: filters must actually be forwarded ------------------------------


def test_remote_list_forwards_metadata_filters(monkeypatch) -> None:
    seen: dict[str, str] = {}

    backend = _remote()

    def _capture(method, path, params=None, **k):
        seen.update(params or {})
        return {"items": [], "total": 0}

    monkeypatch.setattr(backend, "_request", _capture)

    backend.list_memories(
        limit=5, offset=2, metadata_key="profile.tier", metadata_value="pro"
    )

    assert seen["metadata_key"] == "profile.tier"
    assert seen["metadata_value"] == "pro"
    assert seen["limit"] == "5"
    assert seen["offset"] == "2"


def test_remote_list_without_filters_sends_no_filter_params(monkeypatch) -> None:
    seen: dict[str, str] = {}
    backend = _remote()

    def _capture(method, path, params=None, **k):
        seen.update(params or {})
        return {"items": [], "total": 0}

    monkeypatch.setattr(backend, "_request", _capture)
    backend.list_memories(limit=5)

    assert "metadata_key" not in seen and "metadata_value" not in seen


@pytest.mark.parametrize(
    "kwargs,missing",
    [
        ({"metadata_key": "k"}, "metadata_value"),
        ({"metadata_value": "v"}, "metadata_key"),
    ],
)
def test_remote_list_rejects_half_a_filter(monkeypatch, kwargs, missing) -> None:
    """The service 422s on a half-filter; fail before the round trip with a clear message."""
    backend = _remote()
    monkeypatch.setattr(backend, "_request", lambda *a, **k: {"items": [], "total": 0})

    with pytest.raises(ValueError, match=missing):
        backend.list_memories(**kwargs)


# --- The metadata matcher ----------------------------------------------------


@pytest.mark.parametrize(
    "metadata,key,value,expected",
    [
        ({"tier": "pro"}, "tier", "pro", True),
        ({"tier": "pro"}, "tier", "free", False),
        ({"profile": {"tier": "pro"}}, "profile.tier", "pro", True),
        ({"profile": {"tier": "pro"}}, "profile.tier", "free", False),
        (
            {"profile": {"tier": "pro"}},
            "profile",
            "pro",
            False,
        ),  # dict value never matches a scalar
        ({"tier": "pro"}, "missing", "pro", False),
        ({"tier": "pro"}, "tier.deeper", "pro", False),  # descending into a non-dict
        ({"flag": True}, "flag", "true", True),
        ({"flag": True}, "flag", "True", True),
        ({"flag": False}, "flag", "false", True),
        ({"flag": True}, "flag", "false", False),
        # bool is NOT int: Python's True == 1 disagrees with the type-aware store.
        ({"flag": True}, "flag", "1", False),
        ({"count": 1}, "count", "true", False),
        ({"count": 1}, "count", "1", True),
    ],
)
def test_metadata_matches(metadata, key, value, expected) -> None:
    assert _metadata_matches(metadata, key, value) is expected


# --- Local: the three delegations that used to AttributeError ----------------


def test_local_search_by_metadata_filters_instead_of_attributeerror() -> None:
    backend = _local(
        [
            {"item_id": "a", "content": "hit", "metadata": {"tier": "pro"}},
            {"item_id": "b", "content": "miss", "metadata": {"tier": "free"}},
        ]
    )

    out = backend.search_by_metadata("tier", "pro")

    assert [i["item_id"] for i in out] == ["a"]


def test_local_search_by_metadata_uses_star_not_empty_query() -> None:
    """CORE-GATE-1: search("") returns [] — an empty query would match nothing."""
    backend = _local([])
    backend.search_by_metadata("tier", "pro")

    assert backend._mem.search_calls[0][0] == "*"


def test_local_list_memories_paginates_and_filters() -> None:
    hits = [
        {
            "item_id": str(n),
            "content": "c",
            "metadata": {"tier": "pro" if n % 2 == 0 else "free"},
        }
        for n in range(10)
    ]
    backend = _local(hits)

    unfiltered = backend.list_memories(limit=3, offset=0)
    assert [i["item_id"] for i in unfiltered] == ["0", "1", "2"]

    offset_page = backend.list_memories(limit=2, offset=3)
    assert [i["item_id"] for i in offset_page] == ["3", "4"]

    filtered = backend.list_memories(
        limit=10, offset=0, metadata_key="tier", metadata_value="pro"
    )
    assert [i["item_id"] for i in filtered] == ["0", "2", "4", "6", "8"]


def test_local_list_memories_rejects_half_a_filter() -> None:
    backend = _local([])

    with pytest.raises(ValueError, match="metadata_value"):
        backend.list_memories(metadata_key="tier")


def test_local_clear_calls_core_clear() -> None:
    backend = _local([])

    assert "confirm=True" in backend.clear_user_memories(confirm=False)
    assert backend._mem.cleared is False

    backend.clear_user_memories(confirm=True)
    assert backend._mem.cleared is True
