"""CORE-RECALL-BUDGET-1 — the `memory_recall_pack` MCP tool.

Invokes the registered tool via a capturing fake MCP (no FastMCP server) and asserts
registration, backend passthrough, and argument validation. The packing logic itself is
covered in smart-memory-core (``tests/cli/test_recall_pack.py``).

Fake backends are explicit classes, never MagicMock — a MagicMock auto-creates every
attribute, so it cannot distinguish the local and remote branches.
"""

from __future__ import annotations

from unittest.mock import patch

from smartmemory_mcp.tools import memory_tools


def _registered() -> dict:
    captured: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def _decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorator

    memory_tools.register_free(_FakeMCP())
    return captured


class _Backend:
    """Stands in for either backend — the tool calls the same method on both."""

    def __init__(self, result=None):
        self._result = result
        self.calls: list[dict] = []

    def recall_pack(self, **kw):
        self.calls.append(kw)
        return self._result


def _pack(**ov):
    base = {
        "block": "## tier1_user\nSomething the user said.\n",
        "manifest": {
            "budget_tokens": 500,
            "used_tokens": 12,
            "tokenizer": "heuristic:chars/4",
            "query": "budgets",
            "sections": [
                {
                    "name": "tier1_user",
                    "cap_tokens": 175,
                    "used_tokens": 12,
                    "items_packed": 1,
                    "items_compacted": 0,
                    "items_dropped": 0,
                }
            ],
        },
    }
    base.update(ov)
    return base


def test_memory_recall_pack_is_registered_on_the_free_tier():
    """FREE by design — get_working_context precedent. A budgeted context block is the
    thing an agent needs most when it has the least, so it must not sit behind a tier."""
    assert "memory_recall_pack" in _registered()


def test_memory_recall_pack_returns_the_pack_verbatim():
    backend = _Backend(_pack())
    with patch.object(memory_tools, "get_backend", return_value=backend):
        out = _registered()["memory_recall_pack"](budget_tokens=500, query="budgets")

    assert out == _pack()
    assert backend.calls == [
        {"budget_tokens": 500, "query": "budgets", "sections": None}
    ]


def test_memory_recall_pack_forwards_sections():
    backend = _Backend(_pack())
    sections = [{"name": "tier1_user", "cap_tokens": 100}]
    with patch.object(memory_tools, "get_backend", return_value=backend):
        _registered()["memory_recall_pack"](budget_tokens=500, sections=sections)

    assert backend.calls[0]["sections"] == sections
    assert backend.calls[0]["query"] is None


def test_memory_recall_pack_rejects_a_non_positive_budget():
    backend = _Backend(_pack())
    with patch.object(memory_tools, "get_backend", return_value=backend):
        tool = _registered()["memory_recall_pack"]
        assert "error" in tool(budget_tokens=0)
        assert "error" in tool(budget_tokens=-5)

    assert backend.calls == []


def test_memory_recall_pack_rejects_an_unknown_section_name():
    """Never silently skipped: a typo'd section must surface, not shrink the pack."""
    backend = _Backend(_pack())
    with patch.object(memory_tools, "get_backend", return_value=backend):
        out = _registered()["memory_recall_pack"](
            budget_tokens=500, sections=[{"name": "nope", "cap_tokens": 10}]
        )

    assert "error" in out
    assert "nope" in out["error"]
    assert backend.calls == []
