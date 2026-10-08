"""CODE-INDEXER-HARDEN-1 F28: local MCP code_index repo keys (real git + real Lite store).

Two checkouts that share a directory basename must never replace each other's index:
without repo_name each gets its git-remote key; with the same explicit repo_name the
second is refused and the first index stays intact.
"""

import builtins
import shutil
import subprocess

import pytest

from smartmemory.code.repo_key import RepoIdentityConflictError
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import common
from tests.test_code_index_ingest_fixes import _tool

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test_harden_u5",
    "GIT_AUTHOR_EMAIL": "test_harden_u5@example.invalid",
    "GIT_COMMITTER_NAME": "test_harden_u5",
    "GIT_COMMITTER_EMAIL": "test_harden_u5@example.invalid",
}


def _checkout(path, remote, marker):
    path.mkdir(parents=True)
    (path / "svc.py").write_text(f"def {marker}():\n    return 1\n")
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("commit", "-q", "-m", "init"),
        ("remote", "add", "origin", remote),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


@pytest.fixture
def lite(tmp_path, monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "SMARTMEMORY_CODE_CHECKPOINT_DIR", str(tmp_path / "test_harden_u5_checkpoints")
    )
    memory = create_lite_memory(
        str(tmp_path / "test_harden_u5_store"),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    )
    local = LocalBackend()
    local._mem = memory
    monkeypatch.setattr(common, "_backend", local)
    try:
        yield memory
    finally:
        memory.close()
        shutil.rmtree(tmp_path, ignore_errors=True)


def _names_by_repo(memory):
    by_repo = {}
    for node in memory._graph.backend.search_nodes_by_type_or_tag("code"):
        by_repo.setdefault(node["repo"], set()).add(node["name"])
    return by_repo


def test_same_basename_checkouts_get_distinct_remote_keys(lite, tmp_path):
    first = _checkout(
        tmp_path / "one" / "api", "https://github.com/acme/api.git", "acme_only"
    )
    second = _checkout(
        tmp_path / "two" / "api", "git@github.com:other/api.git", "other_only"
    )
    tool = _tool()
    out_first = tool(str(first))
    out_second = tool(str(second))
    assert "Indexed repo 'github.com/acme/api' successfully." in out_first, out_first
    assert "Repo key derived from git remote" in out_first
    assert "Indexed repo 'github.com/other/api' successfully." in out_second, out_second
    by_repo = _names_by_repo(lite)
    assert "acme_only" in by_repo["github.com/acme/api"]
    assert "other_only" in by_repo["github.com/other/api"]
    assert "api" not in by_repo  # the basename is no longer a default key


def test_explicit_name_owned_by_another_checkout_is_refused(lite, tmp_path):
    first = _checkout(
        tmp_path / "one" / "api", "https://github.com/acme/api.git", "acme_only"
    )
    second = _checkout(
        tmp_path / "two" / "api", "https://github.com/other/api.git", "other_only"
    )
    tool = _tool()
    assert "successfully" in tool(str(first), "api")
    before = _names_by_repo(lite)["api"]
    with pytest.raises(
        RepoIdentityConflictError, match="already belongs to a different checkout"
    ):
        tool(str(second), "api")
    assert _names_by_repo(lite)["api"] == before == {"acme_only", "svc"}


def test_without_core_repo_name_is_required(lite, tmp_path, monkeypatch, caplog):
    checkout = _checkout(
        tmp_path / "api", "https://github.com/acme/api.git", "acme_only"
    )
    original_import = builtins.__import__

    def block_repo_key(name, *args, **kwargs):
        if name == "smartmemory.code.repo_key":
            raise ModuleNotFoundError(
                "No module named 'smartmemory.code.repo_key'", name=name
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_repo_key)
    output = _tool()(str(checkout))
    assert output.startswith("Error: repo_name is required"), output
    assert "repo key derivation needs smartmemory-core" in caplog.text
    assert not _names_by_repo(lite)  # nothing was written under a guessed name


def test_remote_backend_upload_carries_provenance_and_identity(tmp_path, monkeypatch):
    """Fix round 1 (finding 8): MCP remote uploads send dirty-aware commit_hash and repo_identity."""
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from smartmemory_mcp.backends.remote import RemoteBackend

    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "SMARTMEMORY_CODE_CHECKPOINT_DIR", str(tmp_path / "test_harden_u5_checkpoints")
    )
    checkout = _checkout(
        tmp_path / "api", "https://user:tok@github.com/acme/api.git", "acme_only"
    )
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (checkout / "svc.py").write_text("def acme_only():\n    return 2\n")
    uploads = []
    app = FastAPI()

    @app.post("/memory/code/index")
    def upload(payload: dict):
        uploads.append(payload)
        return {
            "replaced": True,
            "entities_created": len(payload["entities"]),
            "edges_created": 0,
        }

    with TestClient(app) as client:

        def request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            return client.request(method, url, **kwargs)

        monkeypatch.setattr(httpx, "request", request)
        remote = RemoteBackend(
            api_url="http://testserver",
            api_key="test_harden_u5_key",
            team_id="test_harden_u5_ws",
        )
        remote._session["_bootstrapped"] = True
        monkeypatch.setattr(common, "_backend", remote)
        output = _tool()(str(checkout), "api")
    assert "successfully" in output, output
    (payload,) = uploads
    assert payload["commit_hash"].startswith(f"{head}-dirty-")
    assert payload["repo_identity"] == "remote:github.com/acme/api"
    assert "tok" not in payload["repo_identity"]
    shutil.rmtree(tmp_path, ignore_errors=True)
