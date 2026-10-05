"""Local MCP framework dead-code analysis on the real Lite store."""

import shutil

from fastmcp import FastMCP
from smartmemory.code.indexer import CodeIndexer
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common


def test_local_dead_code_preserves_components_hooks_and_jsx(tmp_path, monkeypatch):
    root = tmp_path / "test_fw_checkout"
    data = tmp_path / "test_fw_store"
    root.mkdir()
    (root / "view.tsx").write_text("""

import express from 'express';
const {request: app} = express();
function uncertainHandler() {}
app.get('header', uncertainHandler);

const Child = () => <span/>;
const Unused = () => <div/>;
function useUnused() { return 1; }
export const View = () => <Child/>;
""")
    memory = None
    try:
        memory = create_lite_memory(
            str(data),
            pipeline_profile=PipelineConfig.lite_hermetic(),
            spawn_worker=False,
        )
        indexer = CodeIndexer(memory._graph, "test_fw_repo", str(root))
        bundle, _ = indexer.prepare_bundle(["typescript"])
        assert indexer.publish_bundle(bundle, generate_embeddings=False).replaced
        local = LocalBackend()
        local._mem = memory
        monkeypatch.setattr(common, "_backend", local)
        registrar = _CapturingRegistrar(FastMCP("test_fw_local"))
        code_tools.register(registrar)
        output = registrar.captured["code_dead_code"].function("test_fw_repo")
        assert "Unused (component)" in output and "useUnused (hook)" in output
        assert "uncertainHandler" not in output
        assert "Child" not in output and "View" not in output
    finally:
        if memory is not None:
            memory.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)


def test_local_falkordb_dead_code_selects_indexed_memory_type(tmp_path, monkeypatch):
    import json
    import os
    from uuid import uuid4

    import pytest
    from redis import Redis
    from redis.exceptions import ConnectionError, TimeoutError

    from smartmemory.tools.factory import create_server_memory
    from smartmemory.scope_provider import DefaultScopeProvider
    from smartmemory.utils import get_config
    from smartmemory.utils.cache import LocalCache

    host = os.environ.get("FALKORDB_HOST", "localhost")
    port = int(os.environ.get("FALKORDB_PORT", "9010"))
    client = Redis(
        host=host,
        port=port,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    try:
        client.ping()
    except (ConnectionError, TimeoutError) as error:
        client.close()
        pytest.skip(f"Local FalkorDB unreachable at {host}:{port}: {error}")
    graph_name = "test_fw_local_" + uuid4().hex
    before = set(client.execute_command("GRAPH.LIST"))
    memory = None
    root = tmp_path / "test_fw_checkout"
    root.mkdir()
    (root / "view.test.tsx").write_text("""
import { test } from 'vitest';

import express from 'express';
const {request: app} = express();
function uncertainHandler() {}
app.get('header', uncertainHandler);

const Child = () => <span/>;
const Unused = () => <div/>;
function useUnused() { return 1; }
function tested() { return 1; }
export const View = () => <Child/>;
test('target', () => tested(), 1000);
""")
    try:
        # The declaration lookup also opens a workspace ontology graph. Configure
        # every constructor for this local server, even without a sibling checkout.
        config = dict(get_config())
        config["graph_db"] = dict(config.get("graph_db", {}))
        config["graph_db"].update(host=host, port=port, main_graph_name=graph_name)
        config_path = tmp_path / "test_fw_config.json"
        config_path.write_text(json.dumps(config))
        monkeypatch.setenv("SMARTMEMORY_CONFIG", str(config_path))
        memory = create_server_memory(
            graph_name=graph_name,
            host=host,
            port=port,
            cache=LocalCache(),
            fresh=False,
            scope_provider=DefaultScopeProvider(
                workspace_id=graph_name,
                tenant_id="test_fw_tenant",
                user_id="test_fw_user",
            ),
            pipeline_profile=PipelineConfig.lite_hermetic(),
            enable_ontology=False,
            observability=False,
        )
        indexer = CodeIndexer(memory._graph, "test_fw_repo", str(root))
        bundle, _ = indexer.prepare_bundle(["typescript"])
        assert indexer.publish_bundle(bundle, generate_embeddings=False).replaced
        stored = memory._graph.backend.search_nodes(
            {"memory_type": "code", "repo": "test_fw_repo"}
        )
        assert {n["name"] for n in stored} >= {
            "Unused",
            "useUnused",
            "Child",
            "View",
            "tested",
        }
        assert all("type" not in n and "tags" not in n for n in stored)
        local = LocalBackend()
        local._mem = memory
        result = local.code_dead_code(repo="test_fw_repo")
        assert {n["name"] for n in result["dead_functions"]} == {"Unused", "useUnused"}
        assert local.code_dead_code(repo="test_fw_other")["count"] == 0
        monkeypatch.setattr(common, "_backend", local)
        registrar = _CapturingRegistrar(FastMCP("test_fw_falkor"))
        code_tools.register(registrar)
        output = registrar.captured["code_dead_code"].function("test_fw_repo")
        assert "Unused (component)" in output and "useUnused (hook)" in output
        assert "uncertainHandler" not in output
        assert "tested" not in output and "Child" not in output and "View" not in output
    finally:
        try:
            if memory is not None:
                memory.close()
        finally:
            try:
                for name in (graph_name, f"ws_{graph_name}_ontology"):
                    if name in client.execute_command("GRAPH.LIST"):
                        client.execute_command("GRAPH.DELETE", name)
                after = set(client.execute_command("GRAPH.LIST"))
                assert not {
                    g
                    for g in after - before
                    if g.startswith(("test_fw_", "ws_test_fw_"))
                }, "Framework graph leak"
            finally:
                client.close()
                shutil.rmtree(root, ignore_errors=True)
