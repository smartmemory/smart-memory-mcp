"""Real SQLite and HTTP regressions for local and standalone MCP indexing."""

import builtins
import shutil

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from smartmemory.code.indexer import CodeIndexer
from smartmemory.graph.backends.sqlite import SQLiteBackend
from smartmemory.graph.smartgraph import SmartGraph
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common


def _tool():
    registrar = _CapturingRegistrar(FastMCP("test_ingest_fixes"))
    code_tools.register(registrar)
    return registrar.captured["code_index"].function


def test_local_partial_parse_retains_prior_sqlite_generation(
    tmp_path, monkeypatch, caplog
):
    root = tmp_path / "test_ingest_checkout"
    root.mkdir()
    (root / "ok.py").write_text("def ok():\n    return 1\n")
    broken = root / "broken.py"
    broken.write_text("def retained():\n    return 2\n")
    memory = create_lite_memory(
        str(tmp_path / "test_ingest_store"),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    )
    try:
        local = LocalBackend()
        local._mem = memory
        monkeypatch.setattr(common, "_backend", local)
        tool = _tool()
        assert "successfully" in tool(str(root), "test_ingest_retention")
        store = memory._graph.backend
        before_nodes = store.search_nodes_by_type_or_tag("code")
        before_edges = store.get_all_edges()
        assert any(node["name"] == "retained" for node in before_nodes)
        broken.write_text("def (broken")
        output = tool(str(root), "test_ingest_retention")
        assert "Error indexing" in output, output
        assert "broken.py" in output and "Prior index was not changed" in output
        assert store.search_nodes_by_type_or_tag("code") == before_nodes
        assert store.get_all_edges() == before_edges
        assert "replacement refused" in caplog.text
    finally:
        memory.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(tmp_path / "test_ingest_store", ignore_errors=True)


@pytest.mark.parametrize("core_installed", [True, False])
def test_remote_parser_dependency_arms(tmp_path, monkeypatch, caplog, core_installed):
    root = tmp_path / "test_ingest_remote"
    root.mkdir()
    (root / "helper.py").write_text("def retained():\n    return 2\n")
    (root / "view.ts").write_text("export function View() { return 1; }\n")
    (root / "excluded").mkdir()
    (root / "excluded" / "ignored.py").write_text("def ignored(): pass\n")
    store = SQLiteBackend()
    graph = SmartGraph(backend=store, enable_caching=False)
    app = FastAPI()
    uploads = []

    @app.post("/memory/code/index")
    def upload(payload: dict):
        uploads.append(payload)
        result = CodeIndexer(graph, payload["repo"], str(root)).publish_bundle(payload)
        return {
            "replaced": result.replaced,
            "entities_created": result.entities_created,
            "edges_created": result.edges_created,
        }

    original_import = builtins.__import__
    calls = []
    if core_installed:
        original_prepare = CodeIndexer.prepare_bundle

        def prepare(self, *args, **kwargs):
            calls.append("core")
            return original_prepare(self, *args, **kwargs)

        monkeypatch.setattr(CodeIndexer, "prepare_bundle", prepare)
    else:
        from smartmemory_mcp.code_parser import CodeParser

        original_parse = CodeParser.parse_file

        def parse(self, *args, **kwargs):
            calls.append("bundled")
            return original_parse(self, *args, **kwargs)

        monkeypatch.setattr(CodeParser, "parse_file", parse)

        def block_core(name, *args, **kwargs):
            if name == "smartmemory" or name.startswith("smartmemory."):
                raise ModuleNotFoundError(
                    "No module named 'smartmemory'", name="smartmemory"
                )
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_core)
    try:
        with TestClient(app) as client:
            # Only transport is redirected. RemoteBackend, parsers and publisher run unchanged.
            def request(method, url, **kwargs):
                kwargs.pop("timeout", None)
                # The upload receiver has core installed, as a real hosted service does.
                with monkeypatch.context() as server_patch:
                    server_patch.setattr(builtins, "__import__", original_import)
                    return client.request(method, url, **kwargs)

            monkeypatch.setattr(httpx, "request", request)
            remote = RemoteBackend(
                api_url="http://testserver",
                api_key="test_ingest_key",
                team_id="test_ingest_workspace",
            )
            remote._session["_bootstrapped"] = True
            monkeypatch.setattr(common, "_backend", remote)
            output = _tool()(str(root), "test_ingest_remote", "excluded")
            assert "successfully" in output, output
            nodes = store.search_nodes_by_type_or_tag("code")
            names = {node["name"] for node in nodes}
            assert "retained" in names and "ignored" not in names
            assert store.get_all_edges()
            if core_installed:
                assert calls == ["core"]
                assert "View" in names
                assert "smartmemory-core" not in output
                assert "Python-only" not in caplog.text
            else:
                assert calls == ["bundled"]
                assert "View" not in names
                assert "smartmemory-core" in output and "TS/JS" in output
                assert any(
                    record.levelname == "WARNING" and "Python-only" in record.message
                    for record in caplog.records
                )
                assert {entity["file_path"] for entity in uploads[0]["entities"]} == {
                    "helper.py"
                }
                (root / "broken.py").write_text("def (broken")
                failed = _tool()(str(root), "test_ingest_remote", "excluded")
                assert "broken.py" in failed and "Prior index was not changed" in failed
                assert len(uploads) == 1
    finally:
        store.close()
        shutil.rmtree(root, ignore_errors=True)


def test_default_code_index_skips_a_failing_js_file_instead_of_refusing(
    tmp_path, monkeypatch, caplog
):
    """CODE-INDEXER-HARDEN-1 F32: no caller language choice means core's default, not an explicit request."""
    root = tmp_path / "test_ingest_default_languages"
    root.mkdir()
    (root / "ok.py").write_text("def ok():\n    return 1\n")
    (root / "bad.js").write_bytes(b"export const x = '\xff\xfe';\n")
    memory = create_lite_memory(
        str(tmp_path / "test_ingest_default_store"),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    )
    seen = []
    try:
        local = LocalBackend()
        local._mem = memory
        original = local.ingest_code

        def recording(**kwargs):
            seen.append(kwargs.get("languages", "absent"))
            return original(**kwargs)

        monkeypatch.setattr(local, "ingest_code", recording)
        monkeypatch.setattr(common, "_backend", local)
        output = _tool()(str(root), "test_ingest_default_languages")
        assert seen == [None]
        assert "successfully" in output, output
        nodes = memory._graph.backend.search_nodes_by_type_or_tag("code")
        assert any(node["name"] == "ok" for node in nodes)
        assert not any(node.get("file_path") == "bad.js" for node in nodes)
        assert "Code file skipped for bad.js" in caplog.text
    finally:
        memory.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(tmp_path / "test_ingest_default_store", ignore_errors=True)
