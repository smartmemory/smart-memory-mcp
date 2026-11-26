"""Run the frozen decision attribution notes through FastMCP on private Lite storage."""

from functools import partial

import anyio
import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.tools import decision_tools
from tests.real_service._attribution_notes import exercise
from tests.test_decision_view_local import world  # noqa: F401


@pytest.mark.asyncio
async def test_stated_attribution_on_lite(world):  # noqa: F811
    """Submit each call through the public MCP schema and dispatcher."""
    server = FastMCP("attribution-lite")
    decision_tools.register(server)
    async with Client(server) as client:

        def call(name, **arguments):
            result = anyio.from_thread.run(client.call_tool, name, arguments)
            return result.content[0].text

        tools = {
            tool.name: partial(call, tool.name) for tool in await client.list_tools()
        }
        await anyio.to_thread.run_sync(exercise, tools)
