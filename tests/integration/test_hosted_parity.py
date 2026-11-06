"""DHRUV-HOSTED-MCP-PARITY-1: local service routes, scoped core and disposable stores.

Opt in with TEST_PARITY_FALKOR_PORT, TEST_PARITY_REDIS_PORT, TEST_PARITY_HTTP_PORT.
Authentication/key lookup are fixture inputs. Retrieval, storage, HTTP, MCP tool
registration and answer composition are real. Only the LLM is deterministic.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import os
import threading
import time
from uuid import uuid4

import pytest


@pytest.fixture
def parity_stack(monkeypatch):
    ports = [
        os.getenv("TEST_PARITY_" + name + "_PORT")
        for name in ("FALKOR", "REDIS", "HTTP")
    ]
    if not all(ports):
        pytest.skip("requires disposable local stores and reserved HTTP port")
    assert all(19000 <= int(p) <= 19999 for p in ports)
    from fastapi import FastAPI
    from fastmcp import FastMCP
    import redis
    import uvicorn
    from memory_service.api.routes import ask as route
    from memory_service.api.routes import crud, routing, workspaces
    from service_common.auth.models import ServiceUser, TenantRole
    from service_common.repositories.scopes import RequestScope
    from service_common.security.scope_provider import MemoryScopeProvider
    from service_common.security.secure_smart_memory import SecureSmartMemory
    from smartmemory.models.memory_item import MemoryItem
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_server_memory
    from smartmemory.utils import llm
    from smartmemory_mcp.hosted.backend import HostedRemoteBackend
    from smartmemory_mcp.hosted.exchange import ExchangeCache
    from smartmemory_mcp.hosted.identity import (
        HostedIdentity,
        VerifiedIdentity,
        current_identity,
    )
    from smartmemory_mcp.hosted.server import _register_hosted_tools
    from smartmemory_mcp.hosted.tools import register_module_tools, apply_allowlist

    graph = "test_dhruv_mcp_parity_" + uuid4().hex
    user = ServiceUser(
        id="test_dhruv_user",
        email="test@example.invalid",
        tenant_id="test_dhruv_tenant",
        tenant_role=TenantRole.OWNER,
    )
    user.account_default_team_id = "test_dhruv_ws"
    user.account_default_status = "usable"
    user.current_workspace_id = "test_dhruv_ws"
    scope = MemoryScopeProvider(
        user,
        RequestScope(
            workspace_id="test_dhruv_ws", user_id=user.id, tenant_id=user.tenant_id
        ),
    )
    graph_client = redis.Redis(host="127.0.0.1", port=int(ports[0]))
    graphs_before = set(graph_client.execute_command("GRAPH.LIST"))
    graph_client.close()
    memory = None
    server = None
    thread = None
    token = None
    try:
        memory = create_server_memory(
            graph_name=graph,
            host="127.0.0.1",
            port=int(ports[0]),
            redis_url=f"redis://127.0.0.1:{ports[1]}/0",
            scope_provider=scope,
            pipeline_profile=PipelineConfig.costless(),
            observability=False,
            tier2_redis_enabled=False,
        )
        from service_common.repositories.clients.mongodb import get_shared_mongo_db
        from service_common.models.workspace import Workspace

        db = get_shared_mongo_db()
        assert db.name.startswith("test_dhruv_mcp_parity")
        db.users.insert_one(
            {"id": user.id, "tenant_id": user.tenant_id, "is_active": True}
        )
        for ws in ("test_dhruv_ws", "test_dhruv_switched"):
            db.workspaces.insert_one(
                Workspace(
                    workspace_id=ws,
                    tenant_id=user.tenant_id,
                    name=ws,
                    owner_user_id=user.id,
                    user_access={user.id: "admin"},
                ).model_dump()
            )
        secure = SecureSmartMemory(
            None, user, smart_memory=memory, scope_provider=scope
        )
        texts = [
            (
                "procedural",
                "Top-dose review: deviation rule v1.0: record all deviations. Superseded on 2026-06-01.",
            ),
            (
                "procedural",
                "Top-dose review: deviation rule v1.1 effective 2026-06-01: record major deviations.",
            ),
            (
                "decision",
                "Top-dose review: site 03 paused recruitment, then pause lifted April 13.",
            ),
            (
                "decision",
                "Top-dose review: enrollment cap 30 per site, raised at sites 01 and 04.",
            ),
            (
                "decision",
                "Top-dose decision: "
                + "The review board reviewed dose safety evidence. " * 8
                + "DECISION: top dose is 80 mg, approved by the review board.",
            ),
            ("episodic", "Top-dose SAE record 1: reviewed patient safety."),
            ("episodic", "Top-dose SAE record 2: reviewed patient safety."),
        ]
        ids = [
            secure.add(
                MemoryItem(
                    content=text, memory_type=kind, metadata={"origin": "api:add"}
                )
            )
            for kind, text in texts
        ]

        def deterministic_llm(**kwargs):
            assert "DECISION: top dose is 80 mg" in kwargs["user_content"]
            return (
                None,
                "ANSWER: The top dose is 80 mg.\nREASONING: The review board decision states 80 mg.",
            )

        monkeypatch.setattr(llm, "call_llm", deterministic_llm)
        monkeypatch.setattr(
            route, "create_secure_smart_memory", lambda supplied: secure
        )
        monkeypatch.setattr(
            route, "caller_llm_key_scope", lambda *a, **kw: nullcontext()
        )
        monkeypatch.setattr(crud, "create_secure_smart_memory", lambda supplied: secure)
        monkeypatch.setattr(
            crud, "caller_llm_key_scope", lambda *a, **kw: nullcontext()
        )
        app = FastAPI()
        save_completed = threading.Event()

        @app.middleware("http")
        async def delay_committed_write(request, call_next):
            response = await call_next(request)
            if (
                request.url.path == "/memory/add"
                and response.status_code < 300
                and request.headers.get("x-test-delay-after-commit") == "1"
            ):
                save_completed.set()
                await asyncio.sleep(0.3)
            return response

        app.add_api_route(
            "/memory/routing/session", routing.put_routing_session, methods=["PUT"]
        )
        app.add_api_route(
            "/memory/routing/explain", routing.explain_routing, methods=["GET"]
        )
        app.add_api_route(
            "/memory/workspaces", workspaces.list_workspaces, methods=["GET"]
        )
        app.dependency_overrides[routing.get_current_user] = lambda: user
        app.dependency_overrides[workspaces.get_service_user] = lambda: user
        app.add_api_route("/memory/search", crud.search_memory, methods=["POST"])
        app.add_api_route("/memory/add", crud.add_memory, methods=["POST"])
        app.add_api_route("/memory/{item_id}", crud.get_memory, methods=["GET"])
        app.include_router(route.router, prefix="/memory")
        for dep in route.router.dependencies:
            app.dependency_overrides[dep.dependency] = lambda: None
        app.dependency_overrides[route.get_scope_provider] = lambda: scope
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=int(ports[2]), log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                pytest.fail("local service failed to start")
            time.sleep(0.05)
        assert server.started
        backend = HostedRemoteBackend(
            f"http://127.0.0.1:{ports[2]}",
            "test_fixture_token",
            "test_dhruv_ws",
            fingerprint="test_dhruv_fp",
            cache=ExchangeCache(),
        )
        backend._session["_bootstrapped"] = True
        backend._parity_save_completed = save_completed
        verified = VerifiedIdentity(
            kind="api_key",
            subject=user.id,
            sm_access_token="test_fixture_token",
            sm_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            user_id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            default_team_id="test_dhruv_ws",
            active_workspace_id="test_dhruv_ws",
            fingerprint="test_dhruv_fp",
        )
        token = current_identity.set(HostedIdentity(verified, "test_dhruv_ws", backend))
        mcp = FastMCP("test-hosted-parity", on_duplicate="replace")
        originals = register_module_tools(mcp)
        _register_hosted_tools(mcp, originals, None)
        apply_allowlist(mcp)
        print(
            f"Preconditions: scoped core={type(memory).__name__}, graph={graph}, rows={len(ids)}, "
            f"service={backend._api_url}, LLM=deterministic, auth=fixture input"
        )
        yield mcp, backend, secure, ids
    finally:
        if token is not None:
            current_identity.reset(token)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        try:
            if memory is not None:
                memory.close()
        finally:
            try:
                from service_common.repositories.clients.mongodb import (
                    get_shared_mongo_db,
                )

                db = get_shared_mongo_db()
                db.client.drop_database(db.name)
            finally:
                client = redis.Redis(host="127.0.0.1", port=int(ports[0]))
                try:
                    for name in client.execute_command("GRAPH.LIST"):
                        if name not in graphs_before:
                            client.execute_command("GRAPH.DELETE", name)
                    assert set(client.execute_command("GRAPH.LIST")) == graphs_before
                finally:
                    client.close()


def test_a_answer_matches_web_route(parity_stack):
    from fastmcp import Client

    mcp, backend, secure, ids = parity_stack

    async def run():
        async with Client(mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "memory_ask" in names
            result = await client.call_tool(
                "memory_ask", {"question": "What is the top-dose decision?"}
            )
            return result.data

    answer = asyncio.run(run())
    assert answer["answer"] == "The top dose is 80 mg."
    assert any(
        row["item_id"] == ids[4] and "DECISION: top dose is 80 mg" in row["content"]
        for row in answer["evidence"]
    )
    assert answer == backend.request(
        "POST",
        "/memory/ask",
        json={"question": "What is the top-dose decision?", "limit": 5},
    )
    from smartmemory_mcp.backends.local import LocalBackend

    local = LocalBackend()
    local._memory = secure._smart_memory
    assert local.ask("What is the top-dose decision?") == answer
    print(answer)


def test_b_recall_preserves_web_ranking_and_full_decision(parity_stack):
    from fastmcp import Client

    mcp, backend, secure, ids = parity_stack
    query = "What is the top-dose decision?"
    web_hits = backend.search(query, top_k=10)
    assert len(web_hits) >= 5
    assert any(item["item_id"] == ids[4] for item in web_hits)

    async def run():
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert (
                tools["memory_recall"].inputSchema["properties"]["top_k"]["default"]
                == 10
            )
            result = await client.call_tool("memory_recall", {"query": query})
            return "\n".join(
                block.text for block in result.content if block.type == "text"
            )

    recalled = asyncio.run(run())
    print("web hits:", [(row["item_id"], row["memory_type"]) for row in web_hits])
    print("recall:", recalled)
    assert "DECISION: top dose is 80 mg" in recalled
    assert recalled.count("\n- ") >= 5
    contents = [row["content"] for row in web_hits]
    assert all(text in recalled for text in contents)
    assert [recalled.index(text) for text in contents] == sorted(
        recalled.index(text) for text in contents
    )


def test_c_unrelated_trial_sites_do_not_get_conflict_warnings(parity_stack):
    from fastmcp import Client
    from smartmemory.models.memory_item import MemoryItem

    mcp, backend, secure, ids = parity_stack
    for site in (1, 2, 4, 5, 6, 7, 8, 9, 10):
        secure.add(
            MemoryItem(
                content=f"Site {site:02} was paused. Enrollment cap is 30.",
                memory_type="semantic",
                origin="api:add",
            )
        )
    receipt = backend.request(
        "POST",
        "/memory/add",
        json={
            "content": "Site 03 was not paused. Enrollment cap is 30.",
            "memory_type": "semantic",
            "metadata": {"force_challenge": True},
        },
        timeout=120,
    )
    assert not backend._fmt_error(receipt), receipt
    item_id = receipt.get("id") or receipt.get("item_id")
    stored = backend.get(item_id)
    assert stored is not None
    print("stored challenge:", stored.get("metadata", {}).get("challenge_result"))
    assert not (stored.get("metadata", {}).get("challenge_result") or {}).get(
        "has_conflicts"
    )

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool("memory_get", {"item_id": item_id})
            return "\n".join(
                block.text for block in result.content if block.type == "text"
            )

    text = asyncio.run(run())
    print("MCP memory_get:", text)
    assert "Site 03 was not paused" in text
    assert "conflicts with" not in text


def test_d_whoami_reads_persisted_switch(parity_stack, monkeypatch):
    from types import SimpleNamespace
    from fastmcp import Client
    from smartmemory_mcp.hosted import middleware as module
    from smartmemory_mcp.hosted.exchange import ExchangeCache
    from smartmemory_mcp.hosted.server import _register_hosted_tools
    from smartmemory_mcp.hosted.tools import register_module_tools, apply_allowlist
    from tests._hosted_fixtures import hosted_config

    mcp, backend, secure, ids = parity_stack
    # Authenticated principal is a fixture input, as it is on the local HTTP app.
    token = SimpleNamespace(
        token="test_fixture_token",
        claims={
            "kind": "api_key",
            "sub": secure.user.id,
            "tenant_id": secure.user.tenant_id,
            "email": secure.user.email,
            "default_team_id": "test_dhruv_ws",
            "active_workspace_id": "test_dhruv_ws",
        },
    )
    monkeypatch.setattr(module, "get_access_token", lambda: token)
    middleware = module.HostedIdentityMiddleware(
        hosted_config(api_url=backend._api_url), cache=ExchangeCache()
    )
    mcp.add_middleware(middleware)
    originals = register_module_tools(mcp)
    _register_hosted_tools(mcp, originals, middleware)
    apply_allowlist(mcp)

    async def run():
        async with Client(mcp) as client:

            def text(result):
                return "\n".join(
                    block.text for block in result.content if block.type == "text"
                )

            before = text(await client.call_tool("whoami", {}))
            switched = text(
                await client.call_tool(
                    "switch_workspace", {"workspace": "test_dhruv_switched"}
                )
            )
            after = text(await client.call_tool("whoami", {}))
            print(before, switched, after, sep="\n")
            assert "Workspace: test_dhruv_ws" in before
            assert "Switched to workspace" in switched
            assert "Workspace: test_dhruv_switched" in after
            assert "Routed by: session" in after

    asyncio.run(run())


def test_e_saved_add_with_lost_receipt_reports_unconfirmed_outcome(
    parity_stack, monkeypatch
):
    import httpx
    from fastmcp import Client

    mcp, backend, secure, ids = parity_stack
    content = "test_dhruv_lost_receipt: the enrollment review completed successfully at site 12."
    headers = backend._headers
    monkeypatch.setattr(
        backend,
        "_headers",
        lambda *a, **kw: {
            **headers(*a, **kw),
            "x-test-delay-after-commit": "1",
        },
    )
    request = httpx.request
    sends = []

    def short_deadline(method, url, **kwargs):
        if method.upper() == "POST" and url == backend._api_url + "/memory/add":
            sends.append(kwargs.get("timeout"))
            kwargs["timeout"] = 0.05  # real local HTTP deadline, not a fake exception
        return request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "request", short_deadline)

    async def run():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "memory_add", {"content": content}, raise_on_error=False
            )
            return "\n".join(
                block.text for block in result.content if block.type == "text"
            )

    message = asyncio.run(run())
    assert backend._parity_save_completed.wait(20), (
        "precondition: write committed before delayed receipt"
    )
    stored = secure.search("test_dhruv_lost_receipt", top_k=10)
    assert sum(item.content == content for item in stored) == 1
    print("MCP outcome:", message, "requests:", sends, "persisted=1")
    assert sends == [120]
    assert len(sends) == 1  # never blindly retry a write with an unknown result
    assert "outcome is unconfirmed" in message
    assert "may have been saved" in message
    assert "backend not reachable" not in message
