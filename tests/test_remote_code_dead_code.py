"""MCP-CODE-DEAD-CODE-BACKEND-DISPATCH-1: one backend seam for dead-code analysis."""

import logging

import pytest
from fastmcp import FastMCP

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common

ANSWER = {
    "dead_functions": [
        {
            "name": "unused",
            "entity_type": "function",
            "file_path": "src/a.py",
            "line_number": 3,
        }
    ],
    "count": 1,
}
BASE = {"repo": "r", "limit": 50}
ROWS = [
    (RemoteBackend, {"repo": "r"}, BASE),
    (HostedRemoteBackend, {"repo": "r"}, BASE),
    (
        RemoteBackend,
        {"repo": "r", "include_exported": True},
        {**BASE, "include_exported": "true"},
    ),
    (
        HostedRemoteBackend,
        {"repo": "r", "production_only": True},
        {**BASE, "production_only": "true"},
    ),
    (
        RemoteBackend,
        {
            "repo": "r",
            "limit": 7,
            "include_exported": True,
            "production_only": True,
            "exclude_decorators": "tool",
        },
        {
            **BASE,
            "limit": 7,
            "include_exported": "true",
            "production_only": "true",
            "exclude_decorators": "tool",
        },
    ),
    (
        HostedRemoteBackend,
        {
            "repo": "r",
            "include_exported": True,
            "production_only": True,
            "exclude_decorators": "tool",
        },
        {
            **BASE,
            "include_exported": "true",
            "production_only": "true",
            "exclude_decorators": "tool",
        },
    ),
]


def _tool(monkeypatch, backend):
    monkeypatch.setattr(common, "_backend", backend)
    registrar = _CapturingRegistrar(FastMCP("test_code_dead_code_remote"))
    code_tools.register(registrar)
    return registrar.captured["code_dead_code"].function


@pytest.mark.parametrize("backend_class,arguments,params", ROWS)
def test_remote_backends_send_one_dead_code_request(
    monkeypatch, backend_class, arguments, params
):
    sent = []
    backend = backend_class.__new__(backend_class)
    monkeypatch.setattr(
        backend,
        "_request",
        lambda method, path, **kw: sent.append((method, path, kw)) or ANSWER,
    )
    method_calls = []
    real_method = backend.code_dead_code

    def through_backend(**kwargs):
        method_calls.append(kwargs)
        return real_method(**kwargs)

    monkeypatch.setattr(backend, "code_dead_code", through_backend)
    text = _tool(monkeypatch, backend)(**arguments)

    expected = [("GET", "/memory/code/dead-code", {"params": params})]
    assert sent == expected
    # This assertion kills the former inline request arm even when its wire parameters match.
    assert method_calls == [
        {"repo": "r", "exclude_decorators": None, "limit": 50, **arguments}
    ]
    assert "1. unused (function)  (src/a.py:3)" in text
    assert "Total: 1 potentially dead callable entities" in text
    options = sorted(
        key for key in ("include_exported", "production_only") if arguments.get(key)
    )
    if options:
        assert f"(options: {', '.join(options)})" in text
    else:
        assert "options:" not in text

    sent.clear()
    assert real_method(**arguments) == ANSWER
    assert sent == expected


@pytest.mark.parametrize(
    "backend_class,answer,expected",
    [
        (RemoteBackend, {"error": "x"}, "Error: x"),
        (HostedRemoteBackend, {"error": "x"}, "Error: x"),
        (RemoteBackend, [], "Unexpected response from API"),
        (HostedRemoteBackend, [], "Unexpected response from API"),
    ],
)
def test_remote_error_responses_are_preserved(
    monkeypatch, backend_class, answer, expected
):
    sent = []
    backend = backend_class.__new__(backend_class)

    def respond(method, path, **kwargs):
        sent.append((method, path, kwargs))
        return answer

    monkeypatch.setattr(backend, "_request", respond)
    assert _tool(monkeypatch, backend)(repo="r") == expected
    assert sent == [("GET", "/memory/code/dead-code", {"params": BASE})]


class NoDeadCodeBackend(RemoteBackend):
    unsupported_capabilities = RemoteBackend.unsupported_capabilities | {
        "code_dead_code"
    }


def test_backend_without_dead_code_analysis_warns_and_sends_nothing(
    monkeypatch, caplog
):
    sent = []
    backend = NoDeadCodeBackend.__new__(NoDeadCodeBackend)
    monkeypatch.setattr(backend, "_request", lambda *a, **kw: sent.append((a, kw)))

    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.tools.code_tools"):
        text = _tool(monkeypatch, backend)(repo="r")

    assert text == "Dead-code analysis is unavailable on this backend."
    assert sent == []
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert caplog.records[0].getMessage() == (
        "code_dead_code: NoDeadCodeBackend has no dead-code analysis, nothing was analyzed"
    )
