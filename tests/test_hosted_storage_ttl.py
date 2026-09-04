"""DefaultTTLWrapper — a floor for TTL-less writes, never a cap (S4).

Round 4 must-fix 1 / round 5 must-fix 1: FastMCP stores refresh state with
whatever lifetime the upstream reported and issues a refresh JWT for the same
lifetime, so ANY cap here would orphan tokens FastMCP still calls valid. The
wrapper therefore substitutes a default only when the write carries no TTL,
which is exactly the DCR client-registration case that would otherwise live
forever.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from key_value.aio.stores.memory import MemoryStore

from smartmemory_mcp.hosted.storage import DEFAULT_CLIENT_RECORD_TTL, DefaultTTLWrapper

DAY = 86400


class _RecordingStore:
    """Captures the exact ttl each write reaches the real store with."""

    def __init__(self) -> None:
        self.puts: list[tuple[str, float | None]] = []
        self.put_manys: list[tuple[list[str], float | None]] = []
        self.reads: list[str] = []

    async def put(self, key, value, *, collection=None, ttl=None) -> None:
        self.puts.append((key, None if ttl is None else float(ttl)))

    async def put_many(self, keys, values, *, collection=None, ttl=None) -> None:
        self.put_manys.append((list(keys), None if ttl is None else float(ttl)))

    async def get(self, key, *, collection=None) -> dict[str, Any] | None:
        self.reads.append(key)
        return {"read": key}

    async def get_many(self, keys, *, collection=None):
        return [{"read": key} for key in keys]

    async def ttl(self, key, *, collection=None):
        return {"read": key}, 123.0

    async def ttl_many(self, keys, *, collection=None):
        return [({"read": key}, 123.0) for key in keys]

    async def delete(self, key, *, collection=None) -> bool:
        return True

    async def delete_many(self, keys, *, collection=None) -> int:
        return len(list(keys))


def test_default_is_thirty_days() -> None:
    assert DEFAULT_CLIENT_RECORD_TTL == 30 * DAY


def test_a_write_with_no_ttl_gets_the_default() -> None:
    store = _RecordingStore()
    wrapper = DefaultTTLWrapper(store, default_ttl=30 * DAY)

    anyio.run(lambda: wrapper.put("client-1", {"a": 1}))

    assert store.puts == [("client-1", float(30 * DAY))]


@pytest.mark.parametrize("ttl", [1.0, 900.0, 180 * DAY, 400 * DAY, 10_000 * DAY])
def test_an_explicit_ttl_is_never_altered(ttl: float) -> None:
    """The whole point: no clamp, in either direction.

    180 d is under the default and 400 d is over it; both must survive
    untouched, or FastMCP's refresh JWT outlives the state behind it.
    """
    store = _RecordingStore()
    wrapper = DefaultTTLWrapper(store, default_ttl=30 * DAY)

    anyio.run(lambda: wrapper.put("k", {"a": 1}, ttl=ttl))

    assert store.puts == [("k", ttl)]


def test_put_many_follows_the_same_rule() -> None:
    store = _RecordingStore()
    wrapper = DefaultTTLWrapper(store, default_ttl=30 * DAY)

    async def body() -> None:
        await wrapper.put_many(["a", "b"], [{"x": 1}, {"y": 2}])
        await wrapper.put_many(["c"], [{"z": 3}], ttl=400 * DAY)

    anyio.run(body)

    assert store.put_manys == [
        (["a", "b"], float(30 * DAY)),
        (["c"], float(400 * DAY)),
    ]


def test_reads_and_deletes_are_plain_passthrough() -> None:
    store = _RecordingStore()
    wrapper = DefaultTTLWrapper(store, default_ttl=30 * DAY)

    async def body() -> tuple[Any, ...]:
        return (
            await wrapper.get("k"),
            await wrapper.get_many(["k"]),
            await wrapper.ttl("k"),
            await wrapper.delete("k"),
            await wrapper.delete_many(["k"]),
        )

    got, got_many, ttl, deleted, deleted_many = anyio.run(body)

    assert got == {"read": "k"}
    assert got_many == [{"read": "k"}]
    assert ttl == ({"read": "k"}, 123.0)
    assert deleted is True
    assert deleted_many == 1
    assert store.reads == ["k"]


def test_round_trip_through_a_real_store() -> None:
    """The value must survive the wrapper unchanged, and land with a live TTL."""
    store = MemoryStore()
    wrapper = DefaultTTLWrapper(store, default_ttl=30 * DAY)

    async def body():
        await wrapper.put("client-1", {"client_id": "abc"})
        value, remaining = await wrapper.ttl("client-1")
        return value, remaining

    value, remaining = anyio.run(body)

    assert value == {"client_id": "abc"}
    assert remaining is not None
    assert 29 * DAY < remaining <= 30 * DAY
