"""DIST-LITE-QUIET-1 — LocalBackend must attribute local writes.

MCP local writes previously landed as origin='unknown' (tier 4, hidden from
recall+search) because LocalBackend.add()/ingest() never set an origin. They
must tag their own producer:

  memory_add     -> mcp:memory_add     (tier 2, recall+search visible)
  memory_ingest  -> mcp:memory_ingest  (tier 2; the /remember skill surface)

A caller-supplied origin is never overwritten.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from smartmemory_mcp.backends.local import LocalBackend


def _backend_with_fake_mem():
    """Build a LocalBackend without touching real smartmemory_app storage."""
    be = LocalBackend.__new__(LocalBackend)
    be._mem = MagicMock()
    return be


def test_add_sets_mcp_memory_add_origin():
    be = _backend_with_fake_mem()
    be._mem.add.return_value = "id-1"

    out = be.add("Alice leads Atlas.", memory_type="semantic")

    assert out == "id-1"
    (item,), _ = be._mem.add.call_args
    assert getattr(item, "origin", None) == "mcp:memory_add", (
        f"memory_add must tag origin 'mcp:memory_add', got {getattr(item, 'origin', None)!r}"
    )


def test_add_respects_caller_supplied_origin():
    be = _backend_with_fake_mem()
    be._mem.add.return_value = "id-2"

    be.add("x", metadata={"origin": "import:vault"})

    (item,), _ = be._mem.add.call_args
    assert getattr(item, "origin", None) == "import:vault"
    # the origin convenience key must not leak into stored metadata
    assert "origin" not in (getattr(item, "metadata", {}) or {})


def test_ingest_passes_mcp_memory_ingest_origin(monkeypatch):
    be = _backend_with_fake_mem()

    captured = {}

    def _fake_ingest(content, memory_type="episodic", **kwargs):
        captured["content"] = content
        captured["kwargs"] = kwargs
        return "id-3"

    fake_storage = types.ModuleType("smartmemory_app.storage")
    fake_storage.ingest = _fake_ingest
    monkeypatch.setitem(sys.modules, "smartmemory_app.storage", fake_storage)

    out = be.ingest("Free-text note.", memory_type="episodic")

    assert out == "id-3"
    assert captured["kwargs"].get("origin") == "mcp:memory_ingest", (
        f"memory_ingest must pass origin 'mcp:memory_ingest', got {captured['kwargs']!r}"
    )


def test_ingest_respects_caller_supplied_origin(monkeypatch):
    be = _backend_with_fake_mem()
    captured = {}

    def _fake_ingest(content, memory_type="episodic", **kwargs):
        captured["kwargs"] = kwargs
        return "id-4"

    fake_storage = types.ModuleType("smartmemory_app.storage")
    fake_storage.ingest = _fake_ingest
    monkeypatch.setitem(sys.modules, "smartmemory_app.storage", fake_storage)

    be.ingest("note", origin="cli:add")
    assert captured["kwargs"].get("origin") == "cli:add"
