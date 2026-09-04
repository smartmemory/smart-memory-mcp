"""CORE-RECALL-CENTERED-1 Phase 2 — standalone MCP `read_around` tool.

Sibling to ``test_get_working_context_standalone.py``: invokes the registered
``read_around`` tool via a capturing fake MCP (no FastMCP server) and asserts
the tool is registered and returns the centered-window contract shape.

The tool is a thin pass-through to ``backend.read_around`` — the budget math and
tenant safety are covered in smart-memory-core (``test_read_around_window.py``)
and smart-memory-service (``tests/security/test_read_around_tenant_isolation.py``)
respectively — so here we verify registration + shape + argument forwarding.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


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


def _window(**overrides) -> dict:
    """A canonical centered-window response (the shape core.build_centered_window returns)."""
    base = {
        "window_items": [
            {
                "item_id": "c1",
                "content": "earlier chunk",
                "memory_type": "semantic",
                "metadata": {},
            },
            {
                "item_id": "c2",
                "content": "matched chunk",
                "memory_type": "semantic",
                "metadata": {},
            },
            {
                "item_id": "c3",
                "content": "later chunk",
                "memory_type": "semantic",
                "metadata": {},
            },
        ],
        "window_text": "earlier chunk\nmatched chunk\nlater chunk",
        "handle": {"item_id": "c2", "conversation_id": "conv-1"},
        "continue_cursor": {"prev_pos": None, "next_pos": None},
        "chars_used": 39,
        "char_budget": 20000,
    }
    base.update(overrides)
    return base


def test_read_around_tool_is_registered():
    """The FREE tier exposes ``read_around``."""
    tools = _registered()
    assert "read_around" in tools


def test_read_around_tool_returns_centered_window_shape():
    """Happy path: the tool returns the full centered-window contract shape."""
    tools = _registered()
    read_around = tools["read_around"]

    fake_backend = MagicMock()
    fake_backend.read_around.return_value = _window()

    with patch(
        "smartmemory_mcp.tools.memory_tools.get_backend", return_value=fake_backend
    ):
        resp = read_around(item_id="c2")

    for key in (
        "window_items",
        "window_text",
        "handle",
        "continue_cursor",
        "chars_used",
        "char_budget",
    ):
        assert key in resp, f"missing contract key {key!r}"
    assert isinstance(resp["continue_cursor"], dict)
    assert {"prev_pos", "next_pos"} <= set(resp["continue_cursor"])
    assert resp["chars_used"] <= resp["char_budget"]
    assert resp["chars_used"] == len(resp["window_text"])
    assert resp["handle"]["item_id"] == "c2"


def test_read_around_tool_forwards_args_to_backend():
    """The tool forwards item_id + budget/ratio/cursor params to the backend unchanged."""
    tools = _registered()
    read_around = tools["read_around"]

    fake_backend = MagicMock()
    fake_backend.read_around.return_value = _window()
    cursor = {"prev_pos": None, "next_pos": 30}

    with patch(
        "smartmemory_mcp.tools.memory_tools.get_backend", return_value=fake_backend
    ):
        read_around(
            item_id="c2",
            char_budget=5000,
            before_ratio=0.5,
            after_ratio=0.5,
            cursor=cursor,
        )

    fake_backend.read_around.assert_called_once_with(
        "c2",
        char_budget=5000,
        before_ratio=0.5,
        after_ratio=0.5,
        cursor=cursor,
    )


def test_read_around_tool_defaults():
    """Calling with only item_id uses the contract defaults (budget 20000, 0.3/0.7)."""
    tools = _registered()
    read_around = tools["read_around"]

    fake_backend = MagicMock()
    fake_backend.read_around.return_value = _window()

    with patch(
        "smartmemory_mcp.tools.memory_tools.get_backend", return_value=fake_backend
    ):
        read_around(item_id="c2")

    _, kwargs = fake_backend.read_around.call_args
    assert kwargs["char_budget"] == 20000
    assert kwargs["before_ratio"] == 0.3
    assert kwargs["after_ratio"] == 0.7
    assert kwargs["cursor"] is None
