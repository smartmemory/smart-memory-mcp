"""Real local scanner and hosted HTTP read against a real scoped SQLite graph."""

import hashlib
import inspect
import json
import shutil
import pytest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from smartmemory.code.effects import scan_effects, snapshot_record
from smartmemory.graph.backends.sqlite import SQLiteBackend
from smartmemory.graph.smartgraph import SmartGraph
from smartmemory_mcp.hosted import effects
from smartmemory_mcp.hosted.tools import _CapturingRegistrar, HOSTED_TOOLS
from smartmemory_mcp.tools import code_tools, common


class Registrar:
    def tool(self, *args, **kwargs):
        return lambda f: f


@pytest.fixture(autouse=True)
def cleanup_effects_test_files(tmp_path):
    """Remove this test's source/checkpoint fixtures even after a failure."""
    try:
        yield
    finally:
        shutil.rmtree(tmp_path)


def test_local_effects_uses_real_parser_without_backend(tmp_path):
    root = tmp_path / "test_fx_repo"
    root.mkdir()
    (root / "app.py").write_text(
        'import sqlite3\ndef write():\n    sqlite3.connect(":memory:").execute("INSERT INTO a VALUES (1)")\n'
    )
    registrar = _CapturingRegistrar(Registrar())
    code_tools.register(registrar)
    result = registrar.captured["code_effects"].function(
        directory=str(root), repo_name="test_fx_repo"
    )
    assert result == scan_effects(root, "test_fx_repo")
    assert any(a["kind"] == "write" for a in result["atoms"])


def test_hosted_effects_scoped_http_sqlite(tmp_path, monkeypatch, caplog):
    from memory_service.api.routes import crud
    from service_common.repositories.scopes import RequestScope
    from service_common.security.scope_provider import MemoryScopeProvider
    from service_common.security.secure_smart_memory import SecureSmartMemory

    root = tmp_path / "test_fx_repo"
    root.mkdir()
    (root / "app.py").write_text('def write():\n    open("test_fx_file", "w")\n')
    output = scan_effects(root, "test_fx_repo")
    record = snapshot_record(output)
    key = record["metadata"]["effects_key"]
    backend = SQLiteBackend()
    from smartmemory.models.memory_item import MemoryItem
    from smartmemory.utils.serialization import MemoryItemSerializer

    for iid, workspace in [
        (record["item_id"], "test_fx_own"),
        ("test_fx_foreign", "test_fx_foreign"),
    ]:
        item = MemoryItem(
            item_id=iid,
            content=record["content"],
            memory_type="fa_snapshot",
            metadata={**record["metadata"], "workspace_id": workspace},
        )
        backend.add_node(
            iid, MemoryItemSerializer.to_storage(item), memory_type="fa_snapshot"
        )
    scope = MemoryScopeProvider(
        user=None,
        request_scope=RequestScope(
            workspace_id="test_fx_own",
            user_id="test_fx_user",
            tenant_id="test_fx_tenant",
            team_id="test_fx_own",
        ),
    )
    # This is the existing retrieval-only SQLite seam. Service startup rejects
    # single-tenant Lite. Actual scope/filter/list/codec/SQLite calls remain real.
    secure = SecureSmartMemory.__new__(SecureSmartMemory)
    secure.scope_provider = scope
    secure._smart_memory = SimpleNamespace(
        _graph=SmartGraph(backend=backend, enable_caching=False)
    )
    monkeypatch.setattr(crud, "create_secure_smart_memory", lambda s: secure)
    app = FastAPI()
    app.include_router(crud.router, prefix="/memory")
    for dep in crud.router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: scope
    app.dependency_overrides[crud.get_scope_provider] = lambda: scope
    registrar = _CapturingRegistrar(Registrar())
    effects.register(registrar)
    tool = registrar.captured["code_effects"].function
    assert "code_effects" in HOSTED_TOOLS
    assert "directory" not in inspect.signature(tool).parameters
    try:
        with TestClient(app) as client:

            class HttpBackend:
                def supports(self, name):
                    return name == "request"

                def request(self, method, path, **kwargs):
                    response = client.request(method, path, **kwargs)
                    assert response.status_code == 200, response.text
                    return response.json()

            monkeypatch.setattr(common, "get_backend", lambda: HttpBackend())
            result = tool(
                repo="test_fx_repo", source_snapshot=output["source_snapshot"]
            )
            assert result["total"] == 1
            assert result["items"][0]["item_id"] == record["item_id"]
            assert result["items"][0]["metadata"]["effects_bundle"] == output
            missing = tool(repo="test_fx_repo", source_snapshot="sha256:" + "0" * 64)
            assert missing["total"] == 0
            assert "Uploaded effects evidence unavailable" in caplog.text
            assert (
                key
                == hashlib.sha256(
                    json.dumps(
                        [output["repo"], output["source_snapshot"]],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
    finally:
        backend.close()


def test_local_unavailable_import_warns_in_standalone_process(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    paths = [
        str(Path(__file__).resolve().parents[1]),
        *[p for p in sys.path if "site-packages" in p],
    ]
    script = """import sys, importlib.util
sys.path[:0] = PATHS
assert importlib.util.find_spec("smartmemory") is None
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import effects_tools
class Registrar:
    def tool(self, *args, **kwargs): return lambda f: f
registrar = _CapturingRegistrar(Registrar())
effects_tools.register(registrar)
result = registrar.captured["code_effects"].function(directory=".")
assert "not installed" in result, result
""".replace("PATHS", repr(paths))
    import os

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Python effects evidence was not obtained" in result.stderr
