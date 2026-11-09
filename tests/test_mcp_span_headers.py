"""INSIGHTS-MCP-TRACES-1: per-tool-call trace headers (remote) and one core span per call (local)."""

import asyncio
import logging
import re
from contextlib import contextmanager

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from smartmemory_mcp.backends import remote as remote_mod
from smartmemory_mcp.backends.dispatch import MODE_ENV
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tool_trace import (
    TOOL_HEADER,
    TRACE_ID_HEADER,
    ToolTraceMiddleware,
    tool_headers,
)


class _Resp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


@pytest.fixture
def sent(monkeypatch):
    """Every HTTP request the remote backend makes, by headers; no network."""
    calls = []

    def _request(method, url, headers=None, **kwargs):
        calls.append(dict(headers or {}))
        return _Resp()

    monkeypatch.setattr(remote_mod.httpx, "request", _request)
    return calls


@pytest.fixture
def backend():
    b = RemoteBackend(api_url="http://127.0.0.1:1", api_key="sk_test", team_id="ws-a")
    b._session["_bootstrapped"] = True
    return b


def _server(backend=None, fail=False) -> FastMCP:
    mcp = FastMCP("trace-test")
    mcp.add_middleware(ToolTraceMiddleware())

    @mcp.tool
    def memory_search(query: str) -> str:
        backend._request("POST", "/memory/search", json={"query": query})
        backend._request("GET", "/memory/items/x")
        return "done"

    @mcp.tool
    def recall(query: str) -> str:
        if fail:
            raise ValueError("boom")
        return f"result:{query}"

    return mcp


def _call(mcp, tool, **args):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def test_remote_tool_call_sends_tool_and_trace_headers(monkeypatch, backend, sent):
    monkeypatch.delenv(MODE_ENV, raising=False)
    mcp = _server(backend)
    _call(mcp, "memory_search", query="q")
    _call(mcp, "memory_search", query="q")

    assert len(sent) == 4
    assert all(h[TOOL_HEADER] == "memory_search" for h in sent)
    trace_ids = [h[TRACE_ID_HEADER] for h in sent]
    assert all(re.fullmatch(r"[0-9a-f]{32}", t) for t in trace_ids)
    # One trace id per tool call: both HTTP calls of a call share it, the next call gets a new one.
    assert trace_ids[0] == trace_ids[1] != trace_ids[2] == trace_ids[3]


def test_headers_absent_outside_a_tool_call(backend):
    assert tool_headers() == {}
    assert TOOL_HEADER not in backend._headers()


def test_remote_mode_opens_no_core_span(monkeypatch, backend, sent):
    monkeypatch.delenv(MODE_ENV, raising=False)
    from smartmemory.observability import tracing

    spans = []
    monkeypatch.setattr(tracing, "_emit_span", spans.append)
    _call(_server(backend), "memory_search", query="q")
    assert spans == []


@pytest.fixture
def local_spans(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "local")
    monkeypatch.setenv("SMARTMEMORY_OBSERVABILITY", "true")
    from smartmemory.observability import tracing

    spans = []
    monkeypatch.setattr(tracing, "_emit_span", spans.append)
    return spans


def test_local_stdio_call_produces_exactly_one_span(local_spans):
    result = _call(_server(), "recall", query="q")
    assert result.data == "result:q"
    mcp_spans = [s for s in local_spans if s["name"].startswith("mcp.")]
    assert len(mcp_spans) == 1
    span = mcp_spans[0]
    assert span["name"] == "mcp.recall"
    assert span["tool"] == "recall" and span["backend"] == "local"
    assert span.get("status") != "error"


def test_local_error_call_still_propagates_and_marks_span(local_spans):
    with pytest.raises(ToolError):
        _call(_server(fail=True), "recall", query="q")
    span = next(s for s in local_spans if s["name"] == "mcp.recall")
    assert span["status"] == "error"
    assert span["error_type"]


def test_exporter_failure_keeps_result_and_warns_once(monkeypatch, caplog):
    monkeypatch.setenv(MODE_ENV, "local")
    from smartmemory.observability import tracing

    class _Span:
        attributes: dict = {}

    @contextmanager
    def broken_span(name, attributes=None, component=None):
        yield _Span()
        raise RuntimeError("exporter down")

    monkeypatch.setattr(tracing, "trace_span", broken_span)
    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.tool_trace"):
        result = _call(_server(), "recall", query="q")
    assert result.data == "result:q"
    lost = [
        r for r in caplog.records if r.getMessage().startswith("mcp span lost: recall")
    ]
    assert len(lost) == 1
