"""CODE-INDEXER-HARDEN-1 U6: ``code_dead_code`` options ``include_exported`` and ``production_only``.

The local arm runs the real Lite store through the tool. The request arm asserts the REST parameters: options are
sent only when set, so a default call issues the same request as before U6.
"""

import shutil
import textwrap

import pytest
from fastmcp import FastMCP
from smartmemory.code.indexer import CodeIndexer
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common

REPO = "test_harden_u6_repo"
FIXTURE = {
    "src/lib.ts": """
        export function exportedTestOnly() { return 1; }
        export function exportedProd() { return 2; }
        export function exportedUnused() { return 3; }
    """,
    "src/app.ts": """
        import { exportedProd } from './lib';
        export function main() { return exportedProd(); }
    """,
    "src/lib.test.ts": """
        import { test } from 'vitest';
        import { exportedTestOnly } from './lib';
        test('exported test only', () => { exportedTestOnly(); });
    """,
    "pkg/__init__.py": "",
    "pkg/core.py": """
        def unexported_test_only():
            return 1


        def run():
            return 2
    """,
    "tests/test_core.py": """
        from pkg.core import unexported_test_only


        def test_unexported_test_only():
            assert unexported_test_only() == 1
    """,
}
EXPECTED = {
    (False, False): {"run"},
    (False, True): {"run", "unexported_test_only"},
    (True, False): {"run", "exportedUnused", "main"},
    (True, True): {
        "run",
        "unexported_test_only",
        "exportedUnused",
        "main",
        "exportedTestOnly",
    },
}


def _register(monkeypatch, backend):
    monkeypatch.setattr(common, "_backend", backend)
    registrar = _CapturingRegistrar(FastMCP("test_harden_u6"))
    code_tools.register(registrar)
    return registrar.captured["code_dead_code"].function


def test_local_lite_store_honours_all_four_combinations(tmp_path, monkeypatch):
    root = tmp_path / "test_harden_u6_checkout"
    data = tmp_path / "test_harden_u6_store"
    for rel, body in FIXTURE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"))
    memory = None
    try:
        memory = create_lite_memory(
            str(data),
            pipeline_profile=PipelineConfig.lite_hermetic(),
            spawn_worker=False,
        )
        indexer = CodeIndexer(memory._graph, REPO, str(root))
        bundle, _ = indexer.prepare_bundle(["python", "typescript"])
        assert indexer.publish_bundle(bundle, generate_embeddings=False).replaced
        local = LocalBackend()
        local._mem = memory
        for (include_exported, production_only), expected in EXPECTED.items():
            options = {
                "include_exported": include_exported,
                "production_only": production_only,
            }
            result = local.code_dead_code(repo=REPO, limit=100, **options)
            assert {n["name"] for n in result["dead_functions"]} == expected, options
            assert result["options"] == options
        assert local.code_dead_code(repo=REPO) == local.code_dead_code(
            repo=REPO, include_exported=False, production_only=False
        )

        tool = _register(monkeypatch, local)
        default = tool(REPO)
        assert "1. run (function)" in default and "options" not in default
        assert "exportedUnused" not in default and "unexported_test_only" not in default
        widest = tool(REPO, include_exported=True, production_only=True)
        assert "(options: include_exported, production_only)" in widest
        for name in EXPECTED[(True, True)]:
            assert f"{name} (function)" in widest
        assert "exportedProd" not in widest
    finally:
        if memory is not None:
            memory.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)


class _RequestBackend:
    """A request-capable backend that records the REST call the tool makes."""

    def __init__(self):
        self.calls = []

    def supports(self, method: str) -> bool:
        return method == "request"

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"repo": "r", "dead_functions": [], "count": 0}


@pytest.mark.parametrize(
    ("arguments", "params"),
    [
        ({}, {"repo": "r", "limit": 50}),
        (
            {"include_exported": True},
            {"repo": "r", "limit": 50, "include_exported": "true"},
        ),
        (
            {"production_only": True},
            {"repo": "r", "limit": 50, "production_only": "true"},
        ),
        (
            {
                "include_exported": True,
                "production_only": True,
                "exclude_decorators": "tool",
            },
            {
                "repo": "r",
                "limit": 50,
                "exclude_decorators": "tool",
                "include_exported": "true",
                "production_only": "true",
            },
        ),
    ],
)
def test_request_path_sends_options_only_when_set(monkeypatch, arguments, params):
    backend = _RequestBackend()
    tool = _register(monkeypatch, backend)
    tool("r", **arguments)
    assert backend.calls == [("GET", "/memory/code/dead-code", {"params": params})]


def test_remote_backend_method_sends_options_only_when_set(monkeypatch):
    calls = []
    backend = RemoteBackend.__new__(RemoteBackend)
    monkeypatch.setattr(
        backend,
        "request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {},
    )
    backend.code_dead_code("r")
    backend.code_dead_code("r", include_exported=True, production_only=True)
    assert calls == [
        ("GET", "/memory/code/dead-code", {"params": {"repo": "r", "limit": 50}}),
        (
            "GET",
            "/memory/code/dead-code",
            {
                "params": {
                    "repo": "r",
                    "limit": 50,
                    "include_exported": "true",
                    "production_only": "true",
                }
            },
        ),
    ]
