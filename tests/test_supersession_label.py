"""DHRUV-MCP-SUPERSESSION-LABEL-1: successors explain which hit they replaced."""

from types import SimpleNamespace

import httpx
import pytest

from smartmemory_mcp.backends.models import normalize_item
from smartmemory_mcp.result_labels import supersession_label
from smartmemory_mcp.tools import memory_tools
from tests.test_hosted_tools_allowlist import (  # noqa: F401
    _Harness,
    _restore_hosted_mode,
    fake_svc_api,
)


OLD_ID = "old-rule-123456789"
LABEL = "[replaces old-rule]"
TOP = {"via_supersession_of": OLD_ID}
NESTED = {"metadata": {"via_supersession_of": OLD_ID}}


def hit(extra):
    return {
        "item_id": "successor-1",
        "content": "Use the lab-confirmed code.",
        "memory_type": "pending",
        "metadata": {},
        **extra,
    }


@pytest.mark.parametrize(
    "item,expected",
    [
        (TOP, " " + LABEL),
        (NESTED, " " + LABEL),
        ({}, ""),
        ({"metadata": None}, ""),
        ({"via_supersession_of": "", **NESTED}, " " + LABEL),
        ({**NESTED, "via_supersession_of": "primary-id"}, " [replaces primary-]"),
    ],
)
def test_marker_locations(item, expected):
    assert supersession_label(item) == expected


@pytest.mark.parametrize(
    "tool,kwargs,extra,expected",
    [
        ("memory_search", {"catalog_mode": True}, TOP, LABEL),
        ("memory_search", {"catalog_mode": True}, NESTED, LABEL),
        ("memory_search", {"catalog_mode": True}, {}, ""),
        ("memory_search", {"catalog_mode": False}, TOP, LABEL),
        ("memory_search", {"catalog_mode": False}, NESTED, LABEL),
        ("memory_search", {"catalog_mode": False}, {}, ""),
        ("memory_recall", {}, TOP, LABEL),
        ("memory_recall", {}, NESTED, LABEL),
        ("memory_recall", {}, {}, ""),
        ("memory_search_advanced", {}, TOP, LABEL),
        (
            "memory_search_advanced",
            {"algorithm": "triangulation_fulldim"},
            NESTED,
            LABEL,
        ),
        (
            "memory_search_by_metadata",
            {"metadata_key": "x", "metadata_value": "y"},
            NESTED,
            LABEL,
        ),
    ],
)
def test_text_renderers(monkeypatch, tool, kwargs, extra, expected):
    captured = {}

    class Capture:
        def tool(self, *args, **kwargs):
            def register(fn):
                captured[fn.__name__] = fn
                return fn

            return register

    memory_tools.register_free(Capture())
    memory_tools.register_pro(Capture())
    row = normalize_item(hit(extra))
    backend = SimpleNamespace(
        search=lambda *a, **kw: [row],
        search_by_metadata=lambda *a, **kw: [row],
        _mem=object(),
    )
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
    if tool == "memory_search_advanced":
        from smartmemory.retrieval import ssg_traversal

        monkeypatch.setattr(
            ssg_traversal,
            "SimilarityGraphTraversal",
            lambda mem: SimpleNamespace(
                query_traversal=lambda *a, **kw: [hit(extra)],
                triangulation_fulldim=lambda *a, **kw: [hit(extra)],
            ),
        )
    args = {} if tool == "memory_search_by_metadata" else {"query": "coding"}
    out = captured[tool](**args, **kwargs)
    assert "Use the lab-confirmed code." in out
    assert out.count("[replaces ") == bool(expected)
    if expected:
        assert expected in out
        assert OLD_ID not in out


@pytest.mark.parametrize(
    "tool,kwargs,extra,expected",
    [
        ("memory_search", {"catalog_mode": True}, TOP, LABEL),
        ("memory_search", {"catalog_mode": True}, NESTED, LABEL),
        ("memory_search", {"catalog_mode": True}, {}, ""),
        ("memory_search", {"catalog_mode": False}, TOP, LABEL),
        ("memory_search", {"catalog_mode": False}, NESTED, LABEL),
        ("memory_search", {"catalog_mode": False}, {}, ""),
        ("memory_recall", {}, TOP, LABEL),
        ("memory_recall", {}, NESTED, LABEL),
        ("memory_recall", {}, {}, ""),
    ],
)
def test_hosted_text(monkeypatch, tool, kwargs, extra, expected):
    inner = fake_svc_api()

    def service(method, url, **kw):
        if url.endswith("/memory/search"):
            return httpx.Response(
                200,
                json={"results": [hit(extra)]},
                request=httpx.Request(method, url),
            )
        return inner(method, url, **kw)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", service)
    harness = _Harness()

    async def call(client):
        session = await harness.session(client)
        return await harness.call(client, session, tool, {"query": "coding", **kwargs})

    result = harness.run(call)
    assert not result.get("isError"), result
    text = "\n".join(part.get("text", "") for part in result["content"])
    assert "Use the lab-confirmed code." in text
    assert text.count("[replaces ") == bool(expected)
    if expected:
        assert expected in text
        assert OLD_ID not in text
