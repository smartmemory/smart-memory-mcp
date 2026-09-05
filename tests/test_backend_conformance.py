"""Audit every direct backend call in tools, including intentionally absent APIs."""

import ast
import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.capabilities import BackendCapabilityMiddleware, TOOL_CAPABILITIES
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from smartmemory_mcp.tools import code_tools, peer_tools, structured_tools


def backend_calls():
    for path in (Path(__file__).parents[1] / "smartmemory_mcp" / "tools").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "backend"
            ):
                yield path.name, node.lineno, node.func.attr


@pytest.mark.parametrize("backend", [LocalBackend, RemoteBackend, HostedRemoteBackend])
def test_every_tool_backend_call_is_implemented_or_explicitly_unsupported(backend):
    calls = list(backend_calls())
    assert calls, "The audit must actually discover backend calls"
    for filename, line, method in calls:
        defined = callable(getattr(backend, method, None))
        assert defined or method in backend.unsupported_capabilities, (
            f"{filename}:{line}: {backend.__name__}.{method} is missing "
            "and has no unsupported capability declaration"
        )
        if not defined:
            assert not backend.supports(method)
            # REST calls have local fallbacks; every other absent operation must
            # have a listing/call gate, not just an unsupported declaration.
            assert method == "request" or method in TOOL_CAPABILITIES.values()


class NoDocumentBackend(RemoteBackend):
    unsupported_capabilities = RemoteBackend.unsupported_capabilities | {
        "ingest_document"
    }


@pytest.mark.parametrize(
    "backend_class", [LocalBackend, RemoteBackend, NoDocumentBackend]
)
def test_capabilities_filter_listings_and_reject_unsupported_calls(
    monkeypatch, backend_class
):
    backend = backend_class.__new__(backend_class)
    monkeypatch.setattr("smartmemory_mcp.capabilities.get_backend", lambda: backend)
    server = FastMCP("capability-test")
    server.add_middleware(BackendCapabilityMiddleware())
    structured_tools.register(server)
    code_tools.register(server)
    peer_tools.register(server)

    async def check():
        async with Client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "code_search" in names  # REST has a local fallback.
            for tool, capability in TOOL_CAPABILITIES.items():
                assert (tool in names) == backend.supports(capability)
                if not backend.supports(capability):
                    with pytest.raises(
                        ToolError, match="not supported by the active backend"
                    ):
                        await client.call_tool(tool, {})

    asyncio.run(check())


@pytest.mark.parametrize("backend_class", [LocalBackend, RemoteBackend])
@pytest.mark.parametrize(
    "status,chunk_ids", [("ingested", ["c1", "c2"]), ("existing", [])]
)
def test_advertised_document_tool_executes(
    monkeypatch, backend_class, status, chunk_ids
):
    backend = backend_class.__new__(backend_class)
    result = {"document_id": "doc1", "chunk_ids": chunk_ids, "status": status}
    if backend_class is LocalBackend:
        backend._mem = Mock(spec=["ingest_document"])
        backend._mem.ingest_document.return_value = result
    else:
        backend._request = Mock(return_value=result)
    monkeypatch.setattr("smartmemory_mcp.capabilities.get_backend", lambda: backend)
    monkeypatch.setattr(structured_tools, "get_backend", lambda: backend)
    server = FastMCP("ingest-test")
    server.add_middleware(BackendCapabilityMiddleware())
    structured_tools.register(server)

    async def check():
        async with Client(server) as client:
            response = await client.call_tool(
                "memory_ingest_document", {"source": "https://example.com/doc"}
            )
            assert (
                response.content[0].text
                == f"Document ingested. ID: doc1, chunks: {len(chunk_ids)}, status: {status}"
            )

    asyncio.run(check())
