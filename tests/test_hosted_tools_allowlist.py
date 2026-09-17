"""The hosted tool surface is an exact, audited allowlist (S5, design.md §4).

Three guarantees, each of which has failed somewhere before:
  1. The advertised set is EXACTLY the literal — a tool added to a shared module
     is hidden hosted by default, not exposed by default (round 2, finding 3).
  2. No hosted tool takes a filesystem path — the container's disk is shared by
     every tenant.
  3. Every advertised tool actually WORKS against a remote backend. A tool that
     needs `_mem`, raises NotImplementedError, or imports `smartmemory` core is
     a broken advertisement, so each one is smoke-called for real, once per
     optional-argument branch (round 2, finding 8).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import anyio
import httpx
import pytest

from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server, hosted_asgi_app
from smartmemory_mcp.hosted.tools import (
    HOSTED_TOOLS,
    PATH_PARAMETER_NAMES,
)

from ._hosted_fixtures import (
    API_KEY,
    INITIALIZE_BODY,
    ME_PAYLOAD,
    auth_me_transport,
    hosted_config,
    mcp_headers,
    memory_store,
)

# Every name design.md §4 excludes, with the reason it was excluded. Listed
# explicitly so re-adding one is a deliberate act with a failing test attached.
EXCLUDED_TOOLS = (
    "login",  # hosted identity comes from the bearer, never a stored key
    "get_working_context",  # imports smartmemory.activation.score
    "memory_clear",  # destructive bulk
    "memory_search_advanced",  # reads backend._mem
    "agent_evaluation_get",  # reads backend._mem
    "reasoning_challenge",  # reads backend._mem
    "reasoning_resolve_conflict",  # reads backend._mem
    "reasoning_proof_tree",  # reads backend._mem
    "reasoning_fuzzy_confidence",  # reads backend._mem
    "reasoning_query",  # symbolic half needs graph.backend
    "reasoning_extract_trace",  # runs the core extractor + an LLM in-process
    "memory_export",  # filesystem path
    "memory_import",  # filesystem path
    "memory_migrate",  # filesystem path
    "code_index",  # filesystem directory
    "code_blame",  # filesystem path
    "code_read_transcript",  # filesystem path
    "memory_auto",  # writes local hook state
    "dev_save_session",  # filesystem paths
    "dev_load_context",  # filesystem paths
    "memory_get_violation_patterns",  # reads local rule files
    "memory_anchor_set",  # AnchorManager needs a core SmartMemory
    "memory_plan_create",  # PlanManager needs a core SmartMemory
    "pattern_query",  # PatternManager reads memory._graph
    "decision_create",  # requires _mem
    "peer_chat",  # parked remotely
    "transcript_search",  # local transcript store
    "memory_ingest_document",  # not audited for hosted
    "memory_ingest_structured",  # not audited for hosted
)

FORBIDDEN_RESULT_FRAGMENTS = (
    "not implemented",
    "requires _mem",
    "_mem",
    "smartmemory package",
    "not installed",
    "no module named",
    "importerror",
)

ITEM = {
    "item_id": "mem-1",
    "content": "The deploy runbook lives in the infra repo.",
    "memory_type": "semantic",
    "metadata": {"source": "test", "steps": []},
    "confidence": 0.9,
}


@pytest.fixture(autouse=True)
def _restore_hosted_mode():
    """`build_hosted_server` flips a process-global; put it back for other files."""
    before = hosted_identity.hosted_mode_enabled()
    try:
        yield
    finally:
        hosted_identity.set_hosted_mode(before)


# --- the fake svc-api -------------------------------------------------------------


def fake_svc_api(seen: list[tuple[str, str]] | None = None):
    """A minimal but valid svc-api for every route a hosted tool can reach."""

    def handler(method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path
        if seen is not None:
            seen.append((method, path))
        request = httpx.Request(method, url)

        def ok(body: Any, status: int = 200) -> httpx.Response:
            return httpx.Response(status, json=body, request=request)

        if path == "/memory/search":
            return httpx.Response(
                200,
                json={"results": [ITEM], "as_of_diagnostics": None},
                headers={"X-Search-Session-Id": "search:ws:abc"},
                request=request,
            )
        if path == "/memory/ingest":
            return ok({"item_id": "mem-1", "status": "ingested"})
        if path == "/memory/ingest/conversation":
            return ok(
                {"conversation_id": "conv-1", "chunks_ingested": 1, "chunks_failed": 0}
            )
        if path == "/memory/add":
            return ok({"item_id": "mem-1"})
        if path == "/memory/read-around":
            return ok({"items": [ITEM], "anchor": "mem-1"})
        if path == "/memory/recall/pack":
            return ok({"sections": [], "budget_tokens": 100, "tokens_used": 0})
        if path == "/memory/policy/bundle":
            return ok({"policies": [], "workflow": None})
        if path == "/memory/list":
            return ok({"items": [ITEM], "total": 1})
        if path == "/memory/by-metadata":
            return ok({"items": [ITEM], "total": 1})
        if path == "/memory/health":
            return ok({"total_items": 1, "items_by_type": {"semantic": 1}})
        if path == "/memory/result-feedback":
            return ok({"result_used_count": 1, "result_shown_count": 1})
        if path == "/memory/teams":
            return ok([{"id": "team-personal", "name": "Personal"}, {"id": "team-B"}])
        if path.startswith("/memory/code/"):
            return ok({"results": [], "total": 0, "files": [], "symbols": []})
        if "/recall-profile" in path:
            return ok({"agent_id": "agent-1", "recall_profile": {}})
        if path.endswith("/explain"):
            return ok({"item_id": "mem-1", "lineage": [], "witness": None})
        if path == "/auth/me":
            return ok(ME_PAYLOAD)
        if method == "DELETE":
            return httpx.Response(204, request=request)
        if method in ("PATCH", "PUT"):
            return ok({"updated": True, "item_id": "mem-1"})
        # A plain GET /memory/{item_id}
        return ok(ITEM)

    return handler


# --- the wire harness -------------------------------------------------------------


class _Harness:
    def __init__(self) -> None:
        self.cfg = hosted_config()
        self.seen: list[tuple[str, str]] = []
        self.app = hosted_asgi_app(
            self.cfg,
            redis_store=memory_store(),
            exchange_cache=ExchangeCache(),
            api_transport=auth_me_transport(),
        )

    def run(self, body):
        async def outer():
            async with self.app.router.lifespan_context(self.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="https://mcp.test",
                ) as client:
                    return await body(client)

        return anyio.run(outer)

    async def session(self, client: httpx.AsyncClient) -> str:
        response = await client.post(
            "/mcp", headers=mcp_headers(API_KEY), json=INITIALIZE_BODY
        )
        assert response.status_code == 200, response.text
        session_id = response.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            headers={**mcp_headers(API_KEY), "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return session_id

    async def call(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await client.post(
            "/mcp",
            headers={**mcp_headers(API_KEY), "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        assert response.status_code == 200, response.text
        payload = _decode(response)
        assert "result" in payload, payload
        return payload["result"]


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Read one JSON-RPC message.

    `hosted_asgi_app` is the production app, so it answers with an SSE stream
    rather than a JSON body. Parsing that here keeps the tests on the object
    uvicorn serves instead of reconfiguring the server for the tests.
    """
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError(f"no SSE data frame in: {response.text[:400]}")
    return response.json()


def _text(result: dict[str, Any]) -> str:
    content = result.get("content") or []
    return "\n".join(part.get("text", "") for part in content)


# --- 1. the exact surface ---------------------------------------------------------


def test_the_advertised_tools_are_exactly_the_allowlist() -> None:
    mcp = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    advertised = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert advertised == set(HOSTED_TOOLS)
    assert len(advertised) == 25


@pytest.mark.parametrize("name", EXCLUDED_TOOLS)
def test_every_excluded_tool_is_absent(name: str) -> None:
    mcp = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    advertised = {tool.name for tool in asyncio.run(mcp.list_tools())}

    assert name not in advertised
    assert name not in HOSTED_TOOLS


# --- 2. no filesystem paths -------------------------------------------------------


def test_no_hosted_tool_takes_a_filesystem_path() -> None:
    """Read off the LIVE schemas, so a new path-taking tool cannot slip in."""
    mcp = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    offenders: list[str] = []
    for tool in asyncio.run(mcp.list_tools()):
        properties = (tool.parameters or {}).get("properties") or {}
        for parameter in properties:
            if parameter.lower() in PATH_PARAMETER_NAMES:
                offenders.append(f"{tool.name}({parameter})")

    assert offenders == [], (
        "hosted tools must not accept filesystem paths; the container disk is "
        f"shared by every tenant: {offenders}"
    )


# --- 3. every advertised tool actually works --------------------------------------

SMOKE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("memory_search", {"query": "runbook"}),
    ("memory_search", {"query": "runbook", "memory_type": "semantic"}),
    ("memory_search", {"query": "runbook", "channel_weights": {"vector": 1.0}}),
    ("memory_search", {"query": "runbook", "top_k": 3, "decompose": True}),
    ("memory_search", {"query": "runbook", "multi_hop": True, "max_hops": 2}),
    ("memory_search", {"query": "runbook", "as_of_date": "2026-01-01"}),
    ("memory_recall", {"query": "runbook"}),
    ("memory_recall", {"query": "runbook", "top_k": 3}),
    ("memory_ingest", {"content": "a note"}),
    ("memory_ingest", {"content": "a note", "memory_type": "episodic"}),
    ("memory_add", {"content": "a note"}),
    ("memory_get", {"item_id": "mem-1"}),
    ("memory_explain", {"memory_id": "mem-1"}),
    ("memory_update", {"item_id": "mem-1", "content": "changed"}),
    ("memory_delete", {"item_id": "mem-1"}),
    ("memory_list", {}),
    ("memory_list", {"limit": 3}),
    ("memory_stats", {}),
    ("memory_distill", {"user_turn": "hi", "assistant_turn": "hello"}),
    (
        "memory_distill",
        {"user_turn": "hi", "assistant_turn": "hello", "session_id": "s-1"},
    ),
    (
        "memory_ingest_conversation",
        {"turns": [{"role": "user", "content": "hi"}]},
    ),
    ("memory_search_by_metadata", {"metadata_key": "source", "metadata_value": "test"}),
    ("memory_recall_pack", {"budget_tokens": 500}),
    ("memory_recall_pack", {"budget_tokens": 500, "query": "runbook"}),
    ("memory_policy_bundle", {}),
    ("memory_policy_bundle", {"workflow": "deploy"}),
    ("memory_feedback", {"search_session_id": "search:ws:abc", "result_used": []}),
    ("read_around", {"item_id": "mem-1"}),
    ("code_search", {"query": "def main"}),
    ("code_search", {"query": "def main", "repo": "smart-memory-mcp", "limit": 5}),
    ("code_dead_code", {"repo": "smart-memory-mcp"}),
    ("code_dead_code", {"repo": "smart-memory-mcp", "exclude_decorators": "tool"}),
    ("code_dependencies", {"entity_name": "main"}),
    ("code_dependencies", {"entity_name": "main", "direction": "in"}),
    ("agent_get_recall_profile", {"agent_id": "agent-1"}),
    (
        "agent_set_recall_profile",
        {"agent_id": "agent-1", "memory_type_weights": {"semantic": 1.0}},
    ),
    ("reasoning_query_traces", {"query": "why"}),
    ("whoami", {}),
    ("switch_team", {"team_id": "team-B"}),
]


def _assert_usable(
    name: str, arguments: dict[str, Any], result: dict[str, Any]
) -> None:
    text = _text(result).lower()
    assert result.get("isError") is not True, (
        f"{name}{arguments} errored: {_text(result)}"
    )
    for fragment in FORBIDDEN_RESULT_FRAGMENTS:
        assert fragment not in text, (
            f"{name}{arguments} is advertised hosted but answered with "
            f"{fragment!r}: {_text(result)}"
        )


def test_every_advertised_tool_is_callable(monkeypatch) -> None:
    harness = _Harness()
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request", fake_svc_api(harness.seen)
    )

    async def body(client):
        session_id = await harness.session(client)
        results = {}
        for name, arguments in SMOKE_CALLS:
            results[(name, json.dumps(arguments, sort_keys=True))] = await harness.call(
                client, session_id, name, arguments
            )
        return results

    results = harness.run(body)

    for (name, raw_arguments), result in results.items():
        _assert_usable(name, json.loads(raw_arguments), result)

    # Every allowlisted tool must appear in the smoke list, or the guarantee is
    # only as good as the coverage.
    smoked = {name for name, _ in SMOKE_CALLS}
    assert smoked == set(HOSTED_TOOLS)


def test_memory_search_returns_the_mocked_content(monkeypatch) -> None:
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(client, session_id, "memory_search", {"query": "q"})

    result = harness.run(body)

    assert "deploy runbook" in _text(result)


def test_memory_recall_returns_a_useful_result(monkeypatch) -> None:
    """Not merely 'did not raise': the default branch must return real content."""
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(client, session_id, "memory_recall", {"query": "q"})

    result = harness.run(body)

    assert result.get("isError") is not True
    assert "deploy runbook" in _text(result)


# --- the explicit hosted refusals -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("memory_search", {"query": "q", "cite": True}, "cite=True"),
        ("memory_recall", {"query": "q", "cite": True}, "cite=True"),
        ("memory_recall", {"query": "q", "session_id": "s-1"}, "session_id"),
    ],
)
def test_core_only_branches_are_refused_explicitly(
    monkeypatch, name: str, arguments: dict[str, Any], expected: str
) -> None:
    """Refused loudly, never silently downgraded (round 3, must-fix 5)."""
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(client, session_id, name, arguments)

    result = harness.run(body)

    assert result["isError"] is True
    text = _text(result)
    assert expected in text
    assert "not available on the hosted server" in text


# --- session tools ----------------------------------------------------------------


def test_whoami_reports_the_hosted_identity(monkeypatch) -> None:
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(client, session_id, "whoami")

    text = _text(harness.run(body))

    assert "dev@test.com" in text
    assert "tenant-1" in text
    assert "team-personal" in text
    assert "api_key" in text


def test_switch_team_then_whoami_reflects_the_new_workspace(monkeypatch) -> None:
    harness = _Harness()
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request", fake_svc_api(harness.seen)
    )

    async def body(client):
        session_id = await harness.session(client)
        before = await harness.call(client, session_id, "whoami")
        switched = await harness.call(
            client, session_id, "switch_team", {"team_id": "team-B"}
        )
        after = await harness.call(client, session_id, "whoami")
        return before, switched, after

    before, switched, after = harness.run(body)

    assert "team-personal" in _text(before)
    assert switched["isError"] is not True
    assert "team-B" in _text(switched)
    assert "team-B" in _text(after)
    # The membership check is a real API call, not a local guess.
    assert ("GET", "/memory/teams") in harness.seen


def test_switch_team_refuses_a_workspace_you_are_not_in(monkeypatch) -> None:
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(
            client, session_id, "switch_team", {"team_id": "someone-elses-team"}
        )

    result = harness.run(body)

    assert result["isError"] is True
    assert "not a member" in _text(result)


def test_a_switched_team_reaches_svc_api_as_the_workspace_header(monkeypatch) -> None:
    headers_seen: list[str] = []

    def spy(method, url, **kwargs):
        headers_seen.append((kwargs.get("headers") or {}).get("X-Workspace-Id", ""))
        return fake_svc_api()(method, url, **kwargs)

    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", spy)

    async def body(client):
        session_id = await harness.session(client)
        await harness.call(client, session_id, "switch_team", {"team_id": "team-B"})
        headers_seen.clear()
        await harness.call(client, session_id, "memory_search", {"query": "q"})

    harness.run(body)

    assert headers_seen == ["team-B"]


# --- /health ----------------------------------------------------------------------


def test_health_is_served_without_authentication() -> None:
    harness = _Harness()

    async def body(client):
        return await client.get("/health")

    response = harness.run(body)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "hosted"}


# --- the core-less degradation is audible -----------------------------------------


def test_a_missing_core_origin_filter_is_logged_not_swallowed(
    monkeypatch, caplog
) -> None:
    """The hosted wheel ships no `smartmemory` core, so the CORE-ORIGIN-1 tier
    filter cannot run. It is the only thing hiding tier-3 speculative items from
    a search (the service's SearchRequest carries no origin field), so skipping
    it must be audible — `except Exception: pass` is the exact shape this repo's
    no-silent-degradation rule forbids."""
    import logging

    from smartmemory_mcp.tools import memory_tools

    monkeypatch.setattr(memory_tools, "_ORIGIN_FILTER_WARNED", False)

    with caplog.at_level(logging.WARNING):
        memory_tools._warn_origin_filter_unavailable(
            ImportError("No module named 'smartmemory'")
        )
        memory_tools._warn_origin_filter_unavailable(ImportError("again"))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "the warning must fire once per process, not per search"
    message = warnings[0].getMessage()
    assert "NOT" in message and "origin tier" in message
    assert "speculative" in message
