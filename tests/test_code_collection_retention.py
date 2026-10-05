"""Collection failures must retain a real SQLite generation on every local surface."""

import builtins
import shutil

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartmemory.code.indexer import CodeIndexer
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import common
from tests.test_code_index_ingest_fixes import _tool


@pytest.mark.parametrize("surface", ["core", "mcp_core", "mcp_bundled"])
@pytest.mark.parametrize("failure", ["directory", "file", "syntax", "decode"])
def test_collection_failure_retains_generation(tmp_path, monkeypatch, surface, failure):
    root = tmp_path / "test_ingest_collection"
    restricted = root / "restricted"
    restricted.mkdir(parents=True)
    hidden = restricted / "hidden.py"
    hidden.write_text("def retained(): return 2\n")
    (root / "ok.py").write_text("def ok(): return 1\n")
    memory = create_lite_memory(
        str(tmp_path / "test_ingest_store"),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    )
    store = memory._graph.backend
    outcomes = []
    uploads = []
    original_import = builtins.__import__
    app = FastAPI()

    @app.post("/memory/code/index")
    def upload(payload: dict):
        uploads.append(payload)
        outcome = CodeIndexer(memory._graph, payload["repo"], str(root)).publish_bundle(
            payload
        )
        outcomes.append(outcome)
        return {
            "replaced": outcome.replaced,
            "entities_created": outcome.entities_created,
            "edges_created": outcome.edges_created,
        }

    try:
        with TestClient(app) as client:
            if surface == "mcp_bundled":

                def block_core(name, *args, **kwargs):
                    if name == "smartmemory" or name.startswith("smartmemory."):
                        raise ModuleNotFoundError(
                            "No module named 'smartmemory'", name="smartmemory"
                        )
                    return original_import(name, *args, **kwargs)

                def request(method, url, **kwargs):
                    kwargs.pop("timeout", None)
                    with monkeypatch.context() as server_patch:
                        server_patch.setattr(builtins, "__import__", original_import)
                        return client.request(method, url, **kwargs)

                monkeypatch.setattr(builtins, "__import__", block_core)
                monkeypatch.setattr(httpx, "request", request)
                backend = RemoteBackend(
                    api_url="http://testserver",
                    api_key="test_ingest_key",
                    team_id="test_ingest_workspace",
                )
                backend._session["_bootstrapped"] = True
                monkeypatch.setattr(common, "_backend", backend)

                def invoke():
                    return _tool()(str(root), "test_ingest_collection")
            elif surface == "mcp_core":
                backend = LocalBackend()
                backend._mem = memory
                original_ingest = backend.ingest_code

                def ingest(**kwargs):
                    outcome = original_ingest(**kwargs)
                    outcomes.append(outcome)
                    return outcome

                monkeypatch.setattr(backend, "ingest_code", ingest)
                monkeypatch.setattr(common, "_backend", backend)

                def invoke():
                    return _tool()(str(root), "test_ingest_collection")
            else:

                def invoke():
                    with memory._di_context():
                        outcome = CodeIndexer(
                            memory._graph, "test_ingest_collection", str(root)
                        ).index(["python"])
                    outcomes.append(outcome)
                    return "successfully" if outcome.replaced else str(outcome.errors)

            assert "successfully" in invoke()
            before_nodes = store.search_nodes_by_type_or_tag("code")
            before_edges = store.get_all_edges()
            assert any(node["name"] == "retained" for node in before_nodes)
            assert before_edges
            outcomes.clear()
            upload_count = len(uploads)
            if failure == "directory":
                restricted.chmod(0)
                with pytest.raises(PermissionError):
                    list(restricted.iterdir())
            elif failure == "file":
                hidden.chmod(0)
                with pytest.raises(PermissionError):
                    hidden.read_bytes()
            elif failure == "syntax":
                hidden.write_text("def (broken")
            else:
                hidden.write_bytes(b"# invalid utf8: \xff\ndef changed(): return 3\n")
            output = invoke()
            failing_path = (
                "restricted" if failure == "directory" else "restricted/hidden.py"
            )
            assert failing_path in output and "Prior index was not changed" in output, (
                output
            )
            assert not any(outcome.replaced for outcome in outcomes)
            if surface != "mcp_bundled":
                assert len(outcomes) == 1 and outcomes[0].replaced is False
            else:
                assert len(uploads) == upload_count, (
                    "Refused standalone replacement must not upload"
                )
            assert store.search_nodes_by_type_or_tag("code") == before_nodes
            assert store.get_all_edges() == before_edges
    finally:
        restricted.chmod(0o755)
        hidden.chmod(0o644)
        memory.close()
        shutil.rmtree(root)
        shutil.rmtree(tmp_path / "test_ingest_store", ignore_errors=True)
