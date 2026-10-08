"""U5 downstream disambiguators over real Lite and FalkorDB stores and service routes."""

from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from smartmemory.code.indexer import CodeIndexer
from smartmemory.code.models import CodeEntity, CodeRelation
from smartmemory.graph.backends.code_publish_lease import lease_keys
from smartmemory.graph.backends.falkordb import FalkorDBBackend
from smartmemory.graph.backends.sqlite import SQLiteBackend
from smartmemory.graph.smartgraph import SmartGraph
from smartmemory.scope_provider import DefaultScopeProvider
from smartmemory.testing.graph_leaks import code_publish_key_graph, delete_graphs
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common

REPO = "test_harden_u5_disambiguation"
WORKSPACE = "test_harden_u5_disambiguation_workspace"


@pytest.fixture(params=["local_sqlite", "local_falkor", "remote_falkor"])
def dependency_tool(request, tmp_path, monkeypatch):
    from memory_service.api.routes import code as routes
    from service_common.auth.core import get_current_user, get_scope_provider

    name = "test_harden_u5_disambiguation_" + uuid4().hex[:8]
    client = redis.Redis(port=9010)
    provider = DefaultScopeProvider(workspace_id=WORKSPACE)
    backend = None
    stack = ExitStack()
    assert not client.exists(name)
    try:
        backend = (
            SQLiteBackend(str(tmp_path / (name + ".db")))
            if request.param == "local_sqlite"
            else FalkorDBBackend(
                host="localhost", port=9010, graph_name=name, scope_provider=provider
            )
        )
        graph = SmartGraph(backend=backend, enable_caching=False)
        exact = CodeEntity("a.helper", "function", "a.py", 1, REPO)
        shadow = CodeEntity("pkg.a.helper", "function", "pkg/a.py", 1, REPO)
        caller = CodeEntity("a.caller", "function", "a.py", 5, REPO)
        other = CodeEntity("pkg.a.other", "function", "pkg/a.py", 5, REPO)
        entities = [exact, shadow, caller, other]
        edges = [
            CodeRelation(caller.item_id, exact.item_id, "CALLS"),
            CodeRelation(other.item_id, shadow.item_id, "CALLS"),
        ]
        assert (
            CodeIndexer(graph, REPO, ".")
            .publish(entities, edges, generate_embeddings=False)
            .replaced
        )
        memory = SimpleNamespace(_graph=graph, _di_context=nullcontext)
        if request.param == "remote_falkor":
            app = FastAPI()
            app.include_router(routes.router, prefix="/memory")
            user = SimpleNamespace(
                id="test_harden_u5_user",
                tenant_id="test_harden_u5_tenant",
                roles=["user"],
                metadata={},
            )
            scope = SimpleNamespace(workspace_id=WORKSPACE, user=user)
            app.dependency_overrides[get_current_user] = lambda: user
            app.dependency_overrides[get_scope_provider] = lambda: scope
            monkeypatch.setattr(
                routes, "create_secure_smart_memory", lambda scope: memory
            )
            monkeypatch.setattr(routes, "get_graph", lambda memory: graph)
            monkeypatch.setattr(
                "service_common.auth.scope.validate_team_membership",
                lambda *a, **k: "test_harden_u5_team",
            )
            monkeypatch.setattr(
                "service_common.auth.scope.extract_team_context",
                lambda *a, **k: ("test_harden_u5_team", "test_harden_u5_tenant"),
            )
            monkeypatch.setattr(
                "service_common.auth.scope.get_scope_flags",
                lambda *a, **k: {"strict_mode": False},
            )
            api = stack.enter_context(TestClient(app))
            remote = RemoteBackend(
                api_url="http://testserver",
                api_key="test_harden_u5_token",
                team_id=WORKSPACE,
            )
            remote._session["_bootstrapped"] = True

            def transport(method, url, **kw):
                kw.pop("timeout", None)
                response = api.request(method, url, **kw)
                # The service's TestClient uses httpx2 in the shared test environment.
                # Adapt transport responses to MCP's httpx without mocking route/backend results.
                import httpx

                return httpx.Response(
                    response.status_code,
                    content=response.content,
                    headers=response.headers,
                    request=httpx.Request(method, url),
                )

            monkeypatch.setattr("httpx.request", transport)
            selected = remote
        else:
            selected = LocalBackend()
            selected._mem = memory
        monkeypatch.setattr(common, "_backend", selected)
        registrar = _CapturingRegistrar(FastMCP("test_harden_u5"))
        code_tools.register(registrar)
        yield registrar.captured["code_dependencies"].function, exact, selected
    finally:
        stack.close()
        if backend is not None:
            backend.close()
        client.delete(*lease_keys(name, WORKSPACE, REPO))
        delete_graphs(client, name)
        assert name.encode() not in client.execute_command("GRAPH.LIST")
        leftover = [
            key.decode()
            for key in client.scan_iter(match="smartmemory:code:publish:*")
            if code_publish_key_graph(key.decode()) == name
        ]
        assert leftover == [], f"leaked claim/publish keys: {leftover}"
        client.close()


@pytest.mark.parametrize("selector", ["file_path", "item_id"])
def test_exact_name_shadowed_by_a_longer_suffix_resolves_through_the_tool(
    dependency_tool, selector
):
    tool, exact, backend = dependency_tool
    ambiguous = tool("a.helper", repo=REPO)
    assert "ambiguous" in ambiguous and "pkg/a.py" in ambiguous and "a.py" in ambiguous
    value = exact.file_path if selector == "file_path" else exact.item_id
    resolved = tool("a.helper", repo=REPO, **{selector: value})
    assert "Entity: a.helper (function) at a.py:1" in resolved
    assert "a.caller (function) CALLS -> this" in resolved
    assert "pkg.a.other" not in resolved
    assert "Error:" not in resolved
    missing = tool("a.helper", repo=REPO, **{selector: "missing"})
    assert "not found" in missing
