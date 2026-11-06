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
    ports = [os.getenv('TEST_PARITY_' + name + '_PORT') for name in ('FALKOR', 'REDIS', 'HTTP')]
    if not all(ports):
        pytest.skip('requires disposable local stores and reserved HTTP port')
    assert all(19000 <= int(p) <= 19999 for p in ports)
    from fastapi import FastAPI
    from fastmcp import FastMCP
    import redis
    import uvicorn
    from memory_service.api.routes import ask as route
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
    from smartmemory_mcp.hosted.identity import HostedIdentity, VerifiedIdentity, current_identity
    from smartmemory_mcp.hosted.server import _register_hosted_tools
    from smartmemory_mcp.hosted.tools import register_module_tools, apply_allowlist

    graph = 'test_dhruv_mcp_parity_' + uuid4().hex
    user = ServiceUser(id='test_dhruv_user', email='test@example.invalid', tenant_id='test_dhruv_tenant',
                       tenant_role=TenantRole.OWNER)
    scope = MemoryScopeProvider(user, RequestScope(workspace_id='test_dhruv_ws', user_id=user.id,
                                                  tenant_id=user.tenant_id))
    memory = create_server_memory(graph_name=graph, host='127.0.0.1', port=int(ports[0]),
                                  redis_url=f'redis://127.0.0.1:{ports[1]}/0', scope_provider=scope,
                                  pipeline_profile=PipelineConfig.costless(), observability=False,
                                  tier2_redis_enabled=False)
    server = None
    thread = None
    token = None
    try:
        secure = SecureSmartMemory(None, user, smart_memory=memory, scope_provider=scope)
        texts = [
            ('procedural', 'Deviation rule v1.0: record all deviations. Superseded on 2026-06-01.'),
            ('procedural', 'Deviation rule v1.1 effective 2026-06-01: record major deviations.'),
            ('decision', 'Site 03 paused recruitment, then pause lifted April 13.'),
            ('decision', 'Enrollment cap 30 per site, raised at sites 01 and 04.'),
            ('decision', 'Top-dose decision: ' + 'The review board reviewed dose safety evidence. ' * 8 +
             'DECISION: top dose is 80 mg, approved by the review board.'),
            ('episodic', 'Top-dose SAE record 1: reviewed patient safety.'),
            ('episodic', 'Top-dose SAE record 2: reviewed patient safety.'),
        ]
        ids = [secure.add(MemoryItem(content=text, memory_type=kind, metadata={'origin': 'api:add'}))
               for kind, text in texts]
        def deterministic_llm(**kwargs):
            assert 'DECISION: top dose is 80 mg' in kwargs['user_content']
            return None, 'ANSWER: The top dose is 80 mg.\nREASONING: The review board decision states 80 mg.'
        monkeypatch.setattr(llm, 'call_llm', deterministic_llm)
        monkeypatch.setattr(route, 'create_secure_smart_memory', lambda supplied: secure)
        monkeypatch.setattr(route, 'caller_llm_key_scope', lambda *a, **kw: nullcontext())
        app = FastAPI()
        app.include_router(route.router, prefix='/memory')
        for dep in route.router.dependencies:
            app.dependency_overrides[dep.dependency] = lambda: None
        app.dependency_overrides[route.get_scope_provider] = lambda: scope
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(ports[2]), log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                pytest.fail('local service failed to start')
            time.sleep(.05)
        assert server.started
        backend = HostedRemoteBackend(f'http://127.0.0.1:{ports[2]}', 'test_fixture_token', 'test_dhruv_ws',
                                      fingerprint='test_dhruv_fp', cache=ExchangeCache())
        backend._session['_bootstrapped'] = True
        verified = VerifiedIdentity(kind='api_key', subject=user.id, sm_access_token='test_fixture_token',
                                    sm_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                                    user_id=user.id, tenant_id=user.tenant_id, email=user.email,
                                    default_team_id='test_dhruv_ws', active_workspace_id='test_dhruv_ws',
                                    fingerprint='test_dhruv_fp')
        token = current_identity.set(HostedIdentity(verified, 'test_dhruv_ws', backend))
        mcp = FastMCP('test-hosted-parity', on_duplicate='replace')
        originals = register_module_tools(mcp)
        _register_hosted_tools(mcp, originals, None)
        apply_allowlist(mcp)
        print(f'Preconditions: scoped core={type(memory).__name__}, graph={graph}, rows={len(ids)}, '
              f'service={backend._api_url}, LLM=deterministic, auth=fixture input')
        yield mcp, backend, secure, ids
    finally:
        if token is not None:
            current_identity.reset(token)
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        memory.close()
        client = redis.Redis(host='127.0.0.1', port=int(ports[0]))
        try:
            for name in client.execute_command('GRAPH.LIST'):
                if name.decode().startswith(graph):
                    client.execute_command('GRAPH.DELETE', name)
            assert not any(name.decode().startswith(graph) for name in client.execute_command('GRAPH.LIST'))
        finally:
            client.close()


def test_a_answer_matches_web_route(parity_stack):
    from fastmcp import Client
    mcp, backend, secure, ids = parity_stack
    async def run():
        async with Client(mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert 'memory_ask' in names
            result = await client.call_tool('memory_ask', {'question': 'What is the top-dose decision?'})
            return result.data
    answer = asyncio.run(run())
    assert answer['answer'] == 'The top dose is 80 mg.'
    assert any(row['item_id'] == ids[4] and 'DECISION: top dose is 80 mg' in row['content']
               for row in answer['evidence'])
    assert answer == backend.request('POST', '/memory/ask', json={'question': 'What is the top-dose decision?', 'limit': 5})
    print(answer)
