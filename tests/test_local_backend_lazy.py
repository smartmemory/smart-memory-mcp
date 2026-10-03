"""Listing capabilities must not initialize local models or storage."""

import asyncio
import builtins
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.capabilities import BackendCapabilityMiddleware, TOOL_CAPABILITIES
from smartmemory_mcp.hosted.identity import current_identity
from smartmemory_mcp.tier import Tier
from smartmemory_mcp.tools import common


@pytest.fixture(autouse=True)
def reset_backends():
    dispatch.reset_backend()
    common.reset_backend()
    yield
    dispatch.reset_backend()
    common.reset_backend()


def test_constructor_and_capabilities_do_not_import_storage(monkeypatch):
    real_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "smartmemory_app.storage":
            raise AssertionError("Constructor or capability check imported storage")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    backend = LocalBackend()
    assert all(backend.supports(method) for method in TOOL_CAPABILITIES.values())
    assert not backend.supports("request")
    assert backend.whoami() == "Local mode — single user."
    assert backend._memory is None


@pytest.mark.parametrize("hide_peer", [False, True])
def test_pro_tools_list_does_not_call_storage_get_memory(monkeypatch, hide_peer):
    import smartmemory_mcp.server as server

    storage = ModuleType("smartmemory_app.storage")
    storage.get_memory = Mock(
        side_effect=AssertionError("tools/list initialized storage")
    )
    config = ModuleType("smartmemory_app.config")
    config.load_config = lambda: SimpleNamespace(mode="local")
    monkeypatch.setitem(sys.modules, "smartmemory_app.storage", storage)
    monkeypatch.setitem(sys.modules, "smartmemory_app.config", config)
    if hide_peer:
        monkeypatch.setattr(
            LocalBackend,
            "unsupported_capabilities",
            frozenset({"request", "peer_chat"}),
        )

    mcp = FastMCP("test_lazy_pro")
    mcp.add_middleware(BackendCapabilityMiddleware())
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "resolve_tier", lambda: Tier.PRO)
    monkeypatch.setattr(server, "_TRANSCRIPT_TOOLS_REGISTERED", False)
    server._register_tools()

    async def list_tools():
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    names = asyncio.run(list_tools())
    assert {"memory_ingest_document", "code_blame", "code_read_transcript"} <= names
    assert ("peer_chat" in names) is not hide_peer
    assert isinstance(dispatch.resolve_backend(), LocalBackend)
    assert dispatch.resolve_backend()._memory is None
    storage.get_memory.assert_not_called()


def test_first_operation_initializes_once_and_caches(monkeypatch):
    storage = ModuleType("smartmemory_app.storage")
    memory = SimpleNamespace(delete=Mock(return_value=True))
    storage.get_memory = Mock(return_value=memory)
    monkeypatch.setitem(sys.modules, "smartmemory_app.storage", storage)
    backend = LocalBackend()
    storage.get_memory.assert_not_called()
    assert backend.delete("test_first") is True
    assert backend.delete("test_second") is True
    storage.get_memory.assert_called_once_with()
    assert [call.args[0] for call in memory.delete.call_args_list] == [
        "test_first",
        "test_second",
    ]
    # Compatibility for callers that previously used the stored getter.
    assert backend._get_memory() is memory


def test_failed_initialization_is_retryable(monkeypatch):
    backend = LocalBackend()
    memory = SimpleNamespace(delete=lambda _: True)
    getter = Mock(side_effect=[RuntimeError("model missing"), memory])
    monkeypatch.setattr(backend, "_get_memory", getter)
    with pytest.raises(RuntimeError, match="model missing"):
        backend.delete("test_failed")
    assert backend._memory is None
    assert backend.delete("test_retry") is True
    assert backend._mem is memory
    assert getter.call_count == 2


def test_concurrent_first_operations_initialize_once(monkeypatch):
    backend = LocalBackend()
    entered = Event()
    release = Event()
    memory = SimpleNamespace(delete=lambda _: True)

    def initialize():
        entered.set()
        assert release.wait(5)
        return memory

    getter = Mock(side_effect=initialize)
    monkeypatch.setattr(backend, "_get_memory", getter)
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(backend.delete, "test_first")
        try:
            assert entered.wait(5)
            rest = [pool.submit(backend.delete, f"test_{i}") for i in range(3)]
        finally:
            release.set()
        assert first.result(timeout=5) is True
        assert all(future.result(timeout=5) is True for future in rest)
    getter.assert_called_once_with()


def test_hosted_resolution_still_precedes_cached_local_backend():
    local = LocalBackend()
    dispatch._backend = local
    common._backend = local
    for tenant in ("test_tenant_a", "test_tenant_b"):
        hosted = SimpleNamespace(tenant=tenant)
        token = current_identity.set(SimpleNamespace(backend=hosted))
        try:
            assert dispatch.resolve_backend() is hosted
            assert common.get_backend() is hosted
        finally:
            current_identity.reset(token)
    assert dispatch.resolve_backend() is local
    assert common.get_backend() is local
    assert local._memory is None
