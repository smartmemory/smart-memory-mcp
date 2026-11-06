"""Real REST controllers, SecureSmartMemory, FalkorDB and Mongo, with in-process HTTP.

Only the transport and request authentication are injected. No storage backend is
mocked. Uses the brief's reusable throwaway servers and deletes exact owned state.
"""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastmcp import FastMCP
import httpx
import pytest
from pymongo import MongoClient
from falkordb import FalkorDB

from smartmemory import SmartMemory
from smartmemory.graph.backends.falkordb import FalkorDBBackend
from smartmemory.graph.smartgraph import SmartGraph
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.stores.vector.backends.usearch import UsearchVectorBackend
from smartmemory.utils.cache import LocalCache
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import common, procedure_tools
from .test_procedure_tools import call, create


@pytest.fixture
def remote_tools(tmp_path, monkeypatch):
    from memory_service.api.routes import crud
    from service_common.auth.models import ServiceUser, TenantRole, TeamRole
    from service_common.auth.tenant import TenantContext
    from service_common.repositories.factories import create_workspace_repository
    from service_common.repositories.scopes import RequestScope
    from service_common.security import SecureSmartMemory
    from service_common.security.scope_provider import MemoryScopeProvider
    from service_common.security.workspace_state import WorkspaceStateAuthority

    prefix = "test_sm_procedure_" + uuid4().hex
    graph_name = prefix + "_graph"
    mongo = MongoClient("mongodb://127.0.0.1:19080", serverSelectionTimeoutMS=3000)
    db = mongo[prefix]
    repo = create_workspace_repository(db=db)
    monkeypatch.setattr(
        "service_common.repositories.factories.create_workspace_repository",
        lambda **kw: repo,
    )
    monkeypatch.setattr(
        "memory_service.services.retrieval_policy.get_shared_mongo_db", lambda: db
    )
    scopes, facades, memories, owned_backends = {}, {}, [], []
    graph_db = FalkorDB(host="127.0.0.1", port=19078)
    graph_before = set(graph_db.list_graphs())
    databases_before = set(mongo.list_database_names())
    try:
        for suffix in ("a", "b"):
            ws = prefix + "_" + suffix
            tenant = prefix + "_tenant"
            uid = prefix + "_user"
            db.workspaces.insert_one(
                {
                    "_id": ws,
                    "workspace_id": ws,
                    "tenant_id": tenant,
                    "is_active": True,
                    "name": ws,
                    "created_by": uid,
                    "owner_user_id": uid,
                }
            )
            user = ServiceUser(
                id=uid,
                email="procedure-test@example.invalid",
                tenant_id=tenant,
                tenant_role=TenantRole.OWNER,
                current_workspace_id=ws,
                current_team_role=TeamRole.ADMIN,
                workspace_permissions={ws: "admin"},
            )
            scope = MemoryScopeProvider(
                user=user,
                request_scope=RequestScope(
                    workspace_id=ws, user_id=uid, tenant_id=tenant, team_id=ws
                ),
                workspace_state=WorkspaceStateAuthority(repo),
            )
            backend = FalkorDBBackend(
                graph_name=graph_name,
                scope_provider=scope,
                host="127.0.0.1",
                port=19078,
            )
            owned_backends.append(backend)
            graph = SmartGraph(
                backend=backend, scope_provider=scope, enable_caching=False
            )
            memory = SmartMemory(
                scope_provider=scope,
                graph=graph,
                enable_ontology=False,
                vector_backend=UsearchVectorBackend(
                    prefix + suffix, str(tmp_path / suffix), dimension=384
                ),
                cache=LocalCache(),
                observability=False,
                tier2_redis_enabled=False,
                pipeline_profile=PipelineConfig.lite(llm_enabled=False),
            )
            memories.append(memory)
            scopes[ws] = scope
            facades[ws] = SecureSmartMemory(
                tenant_context=TenantContext(tenant_id=tenant, user_id=uid, team_id=ws),
                user=user,
                smart_memory=memory,
                scope_provider=scope,
            )
        app = FastAPI()

        def request_scope(request: Request):
            return scopes[request.headers["X-Workspace-Id"]]

        app.dependency_overrides[crud.get_scope_provider] = request_scope
        monkeypatch.setattr(
            crud,
            "create_secure_smart_memory",
            lambda scope: facades[scope.workspace_id],
        )
        # The unchanged, real REST endpoint functions and Pydantic models.
        app.add_api_route("/memory/add", crud.add_memory, methods=["POST"])
        app.add_api_route("/memory/list", crud.list_memories, methods=["GET"])
        app.add_api_route("/memory/{item_id}", crud.get_memory, methods=["GET"])
        app.add_api_route("/memory/{item_id}", crud.update_memory, methods=["PATCH"])
        with TestClient(app) as client:

            def transport(method, url, *, headers=None, timeout=None, **kwargs):
                assert str(url).startswith("http://procedure.test/")
                response = client.request(
                    method,
                    str(url).removeprefix("http://procedure.test"),
                    headers=headers,
                    **kwargs,
                )
                return response

            monkeypatch.setattr(httpx, "request", transport)
            backends = []
            for ws in scopes:
                b = RemoteBackend(
                    api_url="http://procedure.test",
                    api_key="test_sm_procedure_auth",
                    team_id=ws,
                )
                b._session["_bootstrapped"] = True
                backends.append(b)
            mcp = FastMCP("procedure-remote-real-stores")
            procedure_tools.register(mcp)
            yield mcp, backends, facades
    finally:
        for memory in memories:
            memory.close()
        # Injected graph backends are caller-owned. Stop resolver builds before deleting their graph.
        for backend in owned_backends:
            backend.close()
        if graph_name in graph_db.list_graphs():
            graph_db.select_graph(graph_name).delete()
        mongo.drop_database(prefix)
        assert graph_name not in graph_db.list_graphs()
        assert prefix not in mongo.list_database_names()
        assert not (set(graph_db.list_graphs()) - graph_before) & {graph_name}
        assert not (set(mongo.list_database_names()) - databases_before) & {prefix}
        mongo.close()


def test_remote_lifecycle_and_workspace_boundary(remote_tools, monkeypatch):
    from fastmcp.exceptions import ToolError

    mcp, (a, b), facades = remote_tools
    monkeypatch.setattr(common, "_backend", a)
    proc = create(mcp, candidate=True)
    pid = proc["procedure_id"]
    assert call(mcp, "procedure_list", status="candidate")[0]["procedure_id"] == pid
    assert call(mcp, "procedure_promote", procedure_id=pid)["status"] == "active"
    good = call(
        mcp, "procedure_record_outcome", procedure_id=pid, success=True, note="restored"
    )
    assert good["success_count"] == 1
    assert (
        call(mcp, "procedure_list", task="restore database")[0]["procedure_id"] == pid
    )
    assert facades[a._session["team_id"]].get(pid).metadata["steps"] == proc["steps"]
    monkeypatch.setattr(common, "_backend", b)
    assert call(mcp, "procedure_get", procedure_id=pid) is None
    assert call(mcp, "procedure_list") == []
    for tool, args in (
        ("procedure_promote", {}),
        ("procedure_dismiss", {}),
        ("procedure_record_outcome", {"success": False}),
    ):
        with pytest.raises(ToolError):
            call(mcp, tool, procedure_id=pid, **args)
    monkeypatch.setattr(common, "_backend", a)
    after = call(mcp, "procedure_get", procedure_id=pid)
    assert after["success_count"] == 1 and after["failure_count"] == 0
    # Cross a real frozen REST cursor page, with scoped immutable event records.
    from smartmemory.models.memory_item import MemoryItem

    facade = facades[a._session["team_id"]]
    for i in range(100):
        facade.add(
            MemoryItem(
                content=f"test_sm_procedure_remote_page_{i}",
                memory_type="episodic",
                metadata={
                    "procedure_outcome_for": pid,
                    "procedure_outcome_schema": 1,
                    "success": True,
                    "note": "",
                    "recorded_at": "2026-11-06T10:00:00+00:00",
                },
            ),
            _embed=False,
        )
    paged = call(mcp, "procedure_get", procedure_id=pid)
    assert paged["success_count"] == 101 and paged["failure_count"] == 0
    assert len(paged["last_outcomes"]) == 20
    assert call(mcp, "procedure_dismiss", procedure_id=pid)["status"] == "dismissed"
    assert call(mcp, "procedure_list") == []
