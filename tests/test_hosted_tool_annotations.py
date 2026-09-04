"""Hosted MCP tool listing annotations."""

import anyio
import pytest
from fastmcp import Client

from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS

from ._hosted_fixtures import hosted_config, memory_store


@pytest.fixture(autouse=True)
def _restore_hosted_mode():
    """Keep the builder's process-global hosted flag isolated to this test."""
    before = hosted_identity.hosted_mode_enabled()
    try:
        yield
    finally:
        hosted_identity.set_hosted_mode(before)


def test_hosted_tools_have_explicit_directory_annotations() -> None:
    """Directory clients receive titles and safety hints from the live MCP listing."""
    server = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    async def list_tools():
        async with Client(server) as client:
            return await client.list_tools()

    tools = anyio.run(list_tools)
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == set(HOSTED_TOOLS)
    assert len(by_name) == 25
    for name, tool in by_name.items():
        assert tool.title, f"{name} is missing a title"
        assert tool.annotations is not None, f"{name} is missing annotations"
        annotations = tool.annotations.model_dump(by_alias=True)
        assert annotations["readOnlyHint"] is not None, (
            f"{name} is missing readOnlyHint"
        )
        assert annotations["destructiveHint"] is not None, (
            f"{name} is missing destructiveHint"
        )
        assert annotations["openWorldHint"] is False

    assert by_name["memory_delete"].annotations.destructive_hint is True
    assert by_name["memory_search"].annotations.read_only_hint is True
