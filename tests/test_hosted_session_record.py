"""WS-ROUTING-4 (lever 3): the hosted workspace switch lives in svc-api's `routing_sessions`, not in this process.

Before this unit a hosted `switch_workspace` was a per-MCP-session dict in the middleware: a reconnect (new MCP
session id) or a restart of the hosted process lost it (WS-ROUTING-1 gap 6). Now the switch is a
`PUT /memory/routing/session` and every tool call reads it back with `GET /memory/routing/explain?client_key=...`.

Every row drives the real hosted ASGI app over MCP: switch, optionally restart (a fresh app and middleware against the
same svc-api record), open a NEW MCP session, then call `memory_search` and read the workspace header svc-api got.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import pytest

from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import hosted_asgi_app

from ._hosted_fixtures import (
    API_KEY,
    INITIALIZE_BODY,
    RoutingSessionStore,
    auth_me_transport,
    hosted_config,
    mcp_headers,
    memory_store,
)
from .test_hosted_tools_allowlist import _decode, _text, fake_svc_api

DEFAULT = "team-personal"  # ME_PAYLOAD default_team_id


@pytest.fixture(autouse=True)
def _restore_hosted_mode():
    """`build_hosted_server` flips a process-global; put it back for other files."""
    before = hosted_identity.hosted_mode_enabled()
    try:
        yield
    finally:
        hosted_identity.set_hosted_mode(before)


class _Hosted:
    """One hosted process. `explain_status` replaces the explain answer (svc-api failing or too old)."""

    def __init__(self, store: RoutingSessionStore, faults: dict[str, int]) -> None:
        auth = auth_me_transport().handler

        def handler(request: httpx.Request) -> httpx.Response:
            status = faults.get("explain")
            if status and request.url.path == "/memory/routing/explain":
                store.calls.append(
                    ("GET", request.url.path, {"headers": dict(request.headers)})
                )
                return httpx.Response(status, json={"detail": "fault"})
            routed = store.handle(request)
            return routed if routed is not None else auth(request)

        self.app = hosted_asgi_app(
            hosted_config(),
            redis_store=memory_store(),
            exchange_cache=ExchangeCache(),
            api_transport=httpx.MockTransport(handler),
        )

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
        self, client: httpx.AsyncClient, session_id: str, name: str, arguments: dict
    ) -> dict[str, Any]:
        response = await client.post(
            "/mcp",
            headers={**mcp_headers(API_KEY), "mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200, response.text
        payload = _decode(response)
        assert "result" in payload, payload
        return payload["result"]


async def _in(hosted: _Hosted, body):
    async with (
        hosted.app.router.lifespan_context(hosted.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hosted.app), base_url="https://mcp.test"
        ) as client,
    ):
        return await body(client)


@dataclass(frozen=True)
class Row:
    id: str
    switch_to: str | None  # the switch_workspace argument (None: no switch)
    store: dict  # RoutingSessionStore kwargs (forbidden workspaces, bound bearers)
    restart: bool  # the later call runs in a fresh hosted process
    explain_fault: int | None  # explain answers this status AFTER the switch
    switch_error: str | None  # substring of the refused switch's text
    call_error: str | None  # substring of the later call's error text (fail closed)
    workspace: (
        str | None
    )  # X-Workspace-Id svc-api got on the later call (None: no svc-api call happened)
    warning: str | None  # substring of a WARNING the middleware logged


ROWS = [
    Row(
        "reconnect_keeps_the_switch",
        "team-B",
        {},
        False,
        None,
        None,
        None,
        "team-B",
        None,
    ),
    Row(
        "restart_keeps_the_switch", "team-B", {}, True, None, None, None, "team-B", None
    ),
    Row("no_switch_uses_the_default", None, {}, False, None, None, None, DEFAULT, None),
    Row(
        "refused_403_leaves_the_previous",
        "team-B",
        {"forbidden": ("team-B",)},
        False,
        None,
        "not a member",
        None,
        DEFAULT,
        "status=403",
    ),
    Row(
        "bound_key_409_leaves_the_previous",
        "team-B",
        {"bound": (API_KEY,)},
        False,
        None,
        "bound to one workspace",
        None,
        DEFAULT,
        "status=409",
    ),
    Row(
        "explain_5xx_fails_closed",
        "team-B",
        {},
        False,
        503,
        None,
        "workspace selection",
        None,
        "status=503",
    ),
    Row(
        "explain_404_falls_back_with_warning",
        "team-B",
        {},
        False,
        404,
        None,
        None,
        DEFAULT,
        "no routing explain",
    ),
    Row(
        "explain_401_is_unauthorized",
        "team-B",
        {},
        False,
        401,
        None,
        "Unauthorized",
        None,
        "401",
    ),
]


@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
def test_the_hosted_switch_is_the_server_record(row: Row, monkeypatch, caplog) -> None:
    store = RoutingSessionStore(**row.store)
    faults: dict[str, int] = {}
    seen: list[str] = []

    def svc_api(method, url, **kwargs):
        seen.append((kwargs.get("headers") or {}).get("X-Workspace-Id", ""))
        return fake_svc_api()(method, url, **kwargs)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", svc_api)
    first = _Hosted(store, faults)

    async def switch(client):
        session_id = await first.session(client)
        return await first.call(
            client, session_id, "switch_workspace", {"workspace": row.switch_to}
        )

    switched = anyio.run(_in, first, switch) if row.switch_to else None
    if row.switch_error:
        assert switched.get("isError") is True and row.switch_error in _text(
            switched
        ), _text(switched)
    elif switched is not None:
        assert switched.get("isError") is not True, _text(switched)

    if row.explain_fault:
        faults["explain"] = row.explain_fault
    later = _Hosted(store, faults) if row.restart else first
    seen.clear()

    async def search(client):
        session_id = await later.session(client)  # a NEW MCP session id every time
        return await later.call(client, session_id, "memory_search", {"query": "q"})

    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.hosted.middleware"):
        result = anyio.run(_in, later, search)

    if row.call_error:
        assert result.get("isError") is True and row.call_error in _text(result), _text(
            result
        )
    else:
        assert result.get("isError") is not True, _text(result)
    assert (seen[0] if seen else None) == row.workspace
    if row.warning:
        assert any(row.warning in record.getMessage() for record in caplog.records), [
            r.getMessage() for r in caplog.records
        ]
    # The selection read carries the caller's bearer only, never a workspace header (it must not route itself).
    reads = [call for call in store.calls if call[0] == "GET"]
    assert reads, "every hosted call reads the selection from svc-api"
    assert all("x-workspace-id" not in call[2]["headers"] for call in reads)
    if row.switch_to and not row.explain_fault:
        assert all(call[2]["client_key"] == "mcp:test" for call in reads)


def test_no_process_local_session_dict_survives() -> None:
    """The per-MCP-session dict is gone: the only home of a hosted switch is svc-api's record."""
    package = pathlib.Path(__file__).resolve().parents[1] / "smartmemory_mcp"
    hits = [
        str(path)
        for path in package.rglob("*.py")
        if "_session_teams" in path.read_text(encoding="utf-8")
    ]
    assert hits == []
