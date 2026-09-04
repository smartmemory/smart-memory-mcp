"""Public FastMCP tool lookups for synchronous tests."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def _run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine, using a thread when this test already has an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[T] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def tool_fn(name: str) -> Any:
    """Return the function registered for a named FastMCP tool."""

    async def get_tool_fn() -> Any:
        from smartmemory_mcp.server import mcp

        tool = await mcp.get_tool(name)
        if tool is None:
            raise AssertionError(f"{name} tool not registered")
        return tool.fn

    return _run(get_tool_fn())


def tool_names() -> set[str]:
    """Return the names of all registered FastMCP tools."""

    async def get_tool_names() -> set[str]:
        from smartmemory_mcp.server import mcp

        return {tool.name for tool in await mcp.list_tools()}

    return _run(get_tool_names())
