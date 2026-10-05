"""Remote code ingestion preserves the pre-move upload request."""

import builtins
import json
from pathlib import Path

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend


@pytest.mark.parametrize("core_installed", [True, False])
def test_remote_ingest_code_preserves_upload_payload(
    tmp_path, monkeypatch, core_installed
):
    root = tmp_path / "test_int_a_fix2_checkout"
    root.mkdir()
    (root / "helper.py").write_text("def helper(): return 1\n")
    (root / "use.py").write_text(
        "from helper import helper\ndef use(): return helper()\n"
    )
    (root / "view.ts").write_text("export function View() { return 1; }\n")
    (root / "excluded").mkdir()
    (root / "excluded" / "ignored.py").write_text("def ignored(): pass\n")
    expected = json.loads(
        (
            Path(__file__).parent / "fixtures" / "code_upload_before_move.json"
        ).read_text()
    )[str(core_installed)]
    requests = []

    def request(method, path, **kwargs):
        requests.append({"method": method, "path": path, **kwargs})
        return {"replaced": True, "entities_created": 3, "edges_created": 2}

    backend = RemoteBackend(api_url="http://testserver")
    monkeypatch.setattr(backend, "request", request)
    if not core_installed:
        original_import = builtins.__import__

        def block_core(name, *args, **kwargs):
            if name == "smartmemory" or name.startswith("smartmemory."):
                raise ModuleNotFoundError(
                    "No module named 'smartmemory'", name="smartmemory"
                )
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_core)
    result = backend.ingest_code(
        directory=str(root),
        repo="test_int_a_fix2_repo",
        exclude_dirs=["excluded"],
        languages=["python", "typescript"],
    )
    assert requests == [expected]
    assert result.replaced is True
    assert (result.entities_created, result.edges_created) == (3, 2)
    assert result.files_parsed == (3 if core_installed else 2)
    assert result.errors == []
    assert ("Python-only" in result.notice) is not core_installed
