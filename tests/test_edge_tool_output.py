"""Code MCP output through real SQLite memory and in-process HTTP transport."""

import shutil


import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from smartmemory.code.models import code_evidence_properties
from smartmemory.tools.factory import lite_context
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_edge_tool_exposes_persisted_evidence(tmp_path, monkeypatch, mode):
    file = tmp_path / "test_edge_source.py"
    file.write_text("def target():\n    missing()\n")
    with lite_context(str(tmp_path / "test_edge_store")) as memory:
        indexed = memory.ingest_code(str(tmp_path), repo="test_edge_repo")
        assert indexed.replaced, indexed.errors
        target = next(e for e in indexed.entities if e.name == "target")
        backend = LocalBackend()
        backend._mem = memory
        import smartmemory_app.storage as storage

        monkeypatch.setattr(storage, "get_memory", lambda: memory)
        app = FastAPI()

        @app.get("/memory/code/search")
        def search_code():
            props = memory._graph.backend.get_node(target.item_id)
            return [{**props, **code_evidence_properties(props)}]

        with TestClient(app) as client:
            if mode == "hosted":
                backend = RemoteBackend(
                    api_url="http://testserver", api_key="test_edge_key"
                )
                backend._session["_bootstrapped"] = True
                # Route actual httpx requests to the in-process ASGI HTTP transport.
                monkeypatch.setattr(
                    "smartmemory_mcp.backends.remote.httpx.request", client.request
                )
            monkeypatch.setattr(common, "_backend", backend)
            registrar = _CapturingRegistrar(FastMCP("test_edge_tools"))
            code_tools.register(registrar)
            output = registrar.captured["code_search"].function(query="target")
        assert "Evidence: " in output, output
        evidence = json.loads(output.split("Evidence: ", 1)[1])
        assert evidence["item_id"] == target.item_id
        assert evidence["qualified_name"] == "target"
        assert evidence["byte_end"] == target.byte_end
        calls = evidence["call_evidence"]
        calls = json.loads(calls) if isinstance(calls, str) else calls
        assert calls[0]["properties"]["resolution"] == "name_only"
        assert calls[0]["properties"]["unresolved"] is True


@pytest.fixture(autouse=True)
def cleanup_edge_files(tmp_path):
    """Remove task-owned source files and SQLite stores on success and failure."""
    try:
        yield
    finally:
        shutil.rmtree(tmp_path)


def test_legacy_code_warns_about_missing_source_evidence(tmp_path, monkeypatch, caplog):
    with lite_context(str(tmp_path / "test_edge_legacy_store")) as memory:
        memory.ingest_structured(
            dict(
                name="legacy",
                entity_type="function",
                file_path="test_edge_legacy.py",
                line_number=1,
                repo="test_edge_repo",
            ),
            schema="code_entity",
            origin="code:index",
        )
        backend = LocalBackend()
        backend._mem = memory
        import smartmemory_app.storage as storage

        monkeypatch.setattr(storage, "get_memory", lambda: memory)
        monkeypatch.setattr(common, "_backend", backend)
        registrar = _CapturingRegistrar(FastMCP("test_edge_legacy_tools"))
        code_tools.register(registrar)
        output = registrar.captured["code_search"].function(query="legacy")
        assert "legacy" in output
        assert any(
            record.levelname == "WARNING"
            and "source spans and call confidence" in record.message
            for record in caplog.records
        )
