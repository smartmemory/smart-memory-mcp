"""MCP-CODE-DEPS-REMOTE-CONFORMANCE-1: ``code_dependencies`` on the remote backends.

``RemoteBackend`` and ``HostedRemoteBackend`` implement ``code_dependencies`` themselves (it came in with
CODE-INDEXER-HARDEN-1 U5 with only a local method). Each row drives the real tool through the real backend
class and asserts the one REST request that goes out: selectors are sent only when set. The last rows cover the
arm for a backend with no dependency analysis (one WARNING, no request).
"""

import logging

import pytest
from fastmcp import FastMCP

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common

ANSWER = {
    "root": {"entity_type": "function", "file_path": "src/a.py", "line_number": 3},
    "dependencies": [
        {"edge_type": "CALLS", "name": "helper", "entity_type": "function"}
    ],
    "dependents": [],
}
BASE = {"entity_name": "main", "direction": "both"}

# (backend class, tool arguments, expected request params)
ROWS = [
    (RemoteBackend, {"entity_name": "main"}, BASE),
    (HostedRemoteBackend, {"entity_name": "main"}, BASE),
    (
        RemoteBackend,
        {"entity_name": "main", "direction": "dependencies", "repo": "r"},
        {"entity_name": "main", "direction": "dependencies", "repo": "r"},
    ),
    (
        HostedRemoteBackend,
        {"entity_name": "main", "file_path": "src/a.py"},
        {**BASE, "file_path": "src/a.py"},
    ),
    (
        RemoteBackend,
        {
            "entity_name": "main",
            "item_id": "code_1",
            "repo": "r",
            "file_path": "src/a.py",
        },
        {**BASE, "repo": "r", "file_path": "src/a.py", "item_id": "code_1"},
    ),
    (HostedRemoteBackend, {"entity_name": "main", "repo": ""}, BASE),
]


def _tool(monkeypatch, backend):
    monkeypatch.setattr(common, "_backend", backend)
    registrar = _CapturingRegistrar(FastMCP("test_code_deps_remote"))
    code_tools.register(registrar)
    return registrar.captured["code_dependencies"].function


@pytest.mark.parametrize("backend_class,arguments,params", ROWS)
def test_remote_backends_send_one_dependencies_request(
    monkeypatch, backend_class, arguments, params
):
    sent = []
    backend = backend_class.__new__(backend_class)
    monkeypatch.setattr(
        backend,
        "_request",
        lambda method, path, **kw: sent.append((method, path, kw)) or ANSWER,
        raising=False,
    )

    text = _tool(monkeypatch, backend)(**arguments)

    expected = [("GET", "/memory/code/dependencies", {"params": params})]
    assert sent == expected
    assert "Entity: main (function) at src/a.py:3" in text
    assert "CALLS -> helper (function)" in text

    # The backend method is the one seam: calling it directly sends the same request.
    sent.clear()
    assert backend.code_dependencies(**arguments) == ANSWER
    assert sent == expected


class NoDependenciesBackend(RemoteBackend):
    unsupported_capabilities = RemoteBackend.unsupported_capabilities | {
        "code_dependencies"
    }


def test_backend_without_dependency_analysis_warns_and_sends_nothing(
    monkeypatch, caplog
):
    sent = []
    backend = NoDependenciesBackend.__new__(NoDependenciesBackend)
    monkeypatch.setattr(
        backend, "_request", lambda *a, **kw: sent.append(a), raising=False
    )

    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.tools.code_tools"):
        text = _tool(monkeypatch, backend)(entity_name="main")

    assert text == "Dependency analysis is unavailable on this backend."
    assert sent == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "NoDependenciesBackend" in caplog.records[0].getMessage()
