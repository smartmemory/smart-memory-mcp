"""Regression tests for list/stats responses that must not degrade silently."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import FastMCP

from smartmemory_mcp.tools import memory_tools


def _pro_tool(name: str) -> Any:
    """Register PRO tools on an isolated server and return one callable."""
    mcp = FastMCP("list-stats-degradation-test")
    memory_tools.register_pro(mcp)

    async def get_tool() -> Any:
        tool = await mcp.get_tool(name)
        if tool is None:
            raise AssertionError(f"{name} tool not registered")
        return tool.fn

    return asyncio.run(get_tool())


class _Backend:
    """Minimal backend stub for the two collection tools under test."""

    def __init__(
        self,
        *,
        listed: Any = None,
        stats: Any = None,
        stats_error: Exception | None = None,
    ) -> None:
        self._listed = listed
        self._stats = stats
        self._stats_error = stats_error

    def list_memories(self, **kwargs: Any) -> Any:
        return self._listed

    def stats(self) -> Any:
        if self._stats_error is not None:
            raise self._stats_error
        return self._stats


def test_memory_stats_surfaces_backend_failure(monkeypatch) -> None:
    """Assert the failure mechanism, not merely that the displayed count is nonzero."""
    backend = _Backend(stats_error=RuntimeError("API error 500: boom"))
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)

    with pytest.raises(RuntimeError, match="boom"):
        _pro_tool("memory_stats")()


def test_memory_stats_rejects_missing_total(monkeypatch) -> None:
    """A malformed success envelope cannot be presented as a genuine zero."""
    backend = _Backend(stats={"items_by_type": {}})
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)

    with pytest.raises(RuntimeError, match="total_items"):
        _pro_tool("memory_stats")()


def test_memory_list_distinguishes_page_size_from_corpus_total(monkeypatch) -> None:
    backend = _Backend(
        listed={
            "items": [
                {"item_id": "m-1", "content": "first", "memory_type": "semantic"}
            ],
            "total": 4000,
        }
    )
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)

    rendered = _pro_tool("memory_list")(limit=1)

    assert "Showing 1 of 4000 memories" in rendered


def test_memory_list_does_not_guess_total_for_legacy_list(monkeypatch, caplog) -> None:
    backend = _Backend(
        listed=[{"item_id": "m-1", "content": "first", "memory_type": "semantic"}]
    )
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)

    rendered = _pro_tool("memory_list")()

    assert "Returned 1 memories; total unavailable" in rendered
    assert "total" in caplog.text.lower()


def test_memory_list_empty_page_does_not_claim_empty_corpus(monkeypatch) -> None:
    backend = _Backend(listed={"items": [], "total": 4000})
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)

    rendered = _pro_tool("memory_list")(offset=4000)

    assert "No memories returned for this page" in rendered
    assert "4000 memories exist" in rendered
