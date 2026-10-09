"""MCP-LOCAL-BROKEN-TOOLS-1: tools that only error in local mode are not advertised there.

Real FastMCP registration through `server._register_tools` (PRO+ tier), listed through
the real capability middleware, in local, remote and hosted mode.
"""

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.backends.interface import LOCAL_BROKEN_TOOLS
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.capabilities import BackendCapabilityMiddleware, TOOL_CAPABILITIES
from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS
from smartmemory_mcp.tier import Tier
from smartmemory_mcp.tools import common

from ._hosted_fixtures import hosted_config, memory_store

# The 14 tools reproduced as failing in local mode (scratch/2026-10-10-mcp-broken-tools).
BROKEN = (
    "memory_search_advanced",
    "memory_policy_bundle",
    "pattern_list",
    "pattern_query",
    "memory_plan_active",
    "memory_anchor_list",
    "memory_anchor_check_drift",
    "dev_load_context",
    "zettel_clusters",
    "zettel_backlinks",
    "zettel_connections",
    "zettel_discover",
    "memory_plan_create",
    "memory_log_failure",
)


@pytest.fixture(autouse=True)
def reset_backends():
    dispatch.reset_backend()
    common.reset_backend()
    yield
    dispatch.reset_backend()
    common.reset_backend()


def _server(monkeypatch, backend, *, middleware=True):
    """A FastMCP with every PRO+ tool registered the way `server.py` does it."""
    import smartmemory_mcp.server as server

    mcp = FastMCP("test_brokentools")
    middleware_instance = BackendCapabilityMiddleware()
    if middleware:
        mcp.add_middleware(middleware_instance)
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "resolve_tier", lambda: Tier.PRO_PLUS)
    monkeypatch.setattr(server, "_TRANSCRIPT_TOOLS_REGISTERED", False)
    monkeypatch.setattr("smartmemory_mcp.capabilities.get_backend", lambda: backend)
    server._register_tools()
    return mcp


def _names(mcp):
    async def go():
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(go())


def _local():
    return LocalBackend()


def _remote():
    return RemoteBackend(api_key="sk_test_key_123")


def test_table_is_the_audited_set():
    assert set(LOCAL_BROKEN_TOOLS) == set(BROKEN)
    # agent_evaluation_get returns the contract's cold-start None, not an error.
    assert "agent_evaluation_get" not in LOCAL_BROKEN_TOOLS
    assert all(TOOL_CAPABILITIES[tool] == tool for tool in BROKEN)


@pytest.mark.parametrize("tool", BROKEN)
def test_each_tool_is_absent_locally_and_present_remotely(monkeypatch, tool):
    assert tool in _names(_server(monkeypatch, _remote()))
    assert tool not in _names(_server(monkeypatch, _local()))


def test_local_list_differs_from_the_unfiltered_list_by_exactly_the_broken_tools(
    monkeypatch,
):
    everything = _names(_server(monkeypatch, _local(), middleware=False))
    local = _names(_server(monkeypatch, _local()))
    assert everything - local == set(BROKEN)
    assert local <= everything
    # Remote hides only its own pre-existing gates, none of the 14.
    remote = _names(_server(monkeypatch, _remote()))
    assert everything - remote == {"code_blame", "code_read_transcript", "peer_chat"}


@pytest.mark.parametrize("tool", BROKEN)
def test_calling_a_hidden_tool_locally_returns_the_capability_error(monkeypatch, tool):
    mcp = _server(monkeypatch, _local())

    async def go():
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="not supported by the active backend"):
                await client.call_tool(tool, {})

    asyncio.run(go())


def test_listing_warns_once_naming_every_hidden_tool(monkeypatch, caplog):
    mcp = _server(monkeypatch, _local())
    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.capabilities"):
        _names(mcp)
        _names(mcp)
    records = [r for r in caplog.records if r.name == "smartmemory_mcp.capabilities"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    for tool in BROKEN:
        assert tool in records[0].getMessage()
    assert "no attribute '_graph'" in records[0].getMessage()


def test_remote_listing_does_not_warn(monkeypatch, caplog):
    mcp = _server(monkeypatch, _remote())
    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.capabilities"):
        _names(mcp)
    assert not [r for r in caplog.records if r.name == "smartmemory_mcp.capabilities"]


def test_hosted_surface_is_unchanged():
    before = hosted_identity.hosted_mode_enabled()
    try:
        mcp = build_hosted_server(
            hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
        )
        advertised = {tool.name for tool in asyncio.run(mcp.list_tools())}
    finally:
        hosted_identity.set_hosted_mode(before)
    assert advertised == set(HOSTED_TOOLS)
    # Hosted never installs the capability middleware, so a broken-locally tool the
    # hosted allowlist carries stays advertised, and the others stay excluded as before.
    assert advertised & set(BROKEN) == {"memory_policy_bundle"}


def test_hosted_backend_supports_every_broken_tool():
    from smartmemory_mcp.hosted.backend import HostedRemoteBackend

    assert not any(LocalBackend.supports(tool) for tool in BROKEN)
    assert all(HostedRemoteBackend.supports(tool) for tool in BROKEN)
    assert all(RemoteBackend.supports(tool) for tool in BROKEN)
