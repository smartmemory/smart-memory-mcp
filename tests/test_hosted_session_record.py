"""The hosted workspace pick is a server-side record, not process memory (SM20-WS-ROUTING-PARITY-1, shape B).

`switch_team` writes `PUT /memory/routing/session`; the middleware reads `GET /memory/routing/session` before it
builds each call's backend and sends the pick as `X-Workspace-Id`. svc-api is a `RoutingSessionStore` behind an
`httpx.MockTransport`: it outlives middleware instances the way the Mongo record outlives a process.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio
import pytest

from smartmemory_mcp.hosted.middleware import HostedIdentityMiddleware

from ._hosted_fixtures import API_KEY, RoutingSessionStore
from .test_hosted_identity import (
    OAUTH_BEARER,
    OAUTH_WORKSPACE,
    SM_JWT,
    _Harness,
    _run,
    _text,
)
from .test_hosted_tools_allowlist import _Harness as _ServerHarness
from .test_hosted_tools_allowlist import fake_svc_api

# One row per scenario. `steps` run in order on a fresh server (instance 1); `restart` then builds a NEW server
# instance on the same record store and a NEW MCP session id before the final probe. `team` is the workspace the
# final probe's backend (and so its X-Workspace-Id) carries; `warns` is a WARNING fragment that must be logged.
ROWS: list[dict[str, Any]] = [
    {
        "id": "switch-then-next-call",
        "steps": ["B"],
        "restart": False,
        "store": {},
        "team": "team-B",
        "puts": 1,
    },
    {
        "id": "restart-new-instance-new-session",
        "steps": ["B"],
        "restart": True,
        "store": {},
        "team": "team-B",
        "puts": 1,
    },
    {
        "id": "no-switch-default",
        "steps": [],
        "restart": True,
        "store": {},
        "team": OAUTH_WORKSPACE,
        "puts": 0,
    },
    {
        "id": "non-member-refused-nothing-stored",
        "steps": ["B"],
        "restart": True,
        "store": {"forbidden": ("team-B",)},
        "team": OAUTH_WORKSPACE,
        "puts": 1,
        "refused": True,
    },
    {
        "id": "expired-null-warns-and-uses-default",
        "steps": [],
        "restart": True,
        "seed": "team-B",
        "store": {"expired": (SM_JWT,)},
        "team": OAUTH_WORKSPACE,
        "warns": f"no saved pick, or it expired); using the default workspace {OAUTH_WORKSPACE}",
    },
    {
        "id": "absent-warns-and-uses-default",
        "steps": [],
        "restart": True,
        "store": {},
        "team": OAUTH_WORKSPACE,
        "warns": f"no saved pick, or it expired); using the default workspace {OAUTH_WORKSPACE}",
    },
    {
        "id": "read-503-warns-and-uses-default",
        "steps": [],
        "restart": True,
        "seed": "team-B",
        "store": {"get_status": 503},
        "team": OAUTH_WORKSPACE,
        "warns": f"read failure, status=503); using the default workspace {OAUTH_WORKSPACE}",
    },
    {
        "id": "read-transport-error-warns-and-uses-default",
        "steps": [],
        "restart": True,
        "seed": "team-B",
        "store": {"get_raises": True},
        "team": OAUTH_WORKSPACE,
        "warns": f"read failure, svc-api unreachable); using the default workspace {OAUTH_WORKSPACE}",
    },
    {
        "id": "read-404-warns-and-uses-default",
        "steps": [],
        "restart": True,
        "seed": "team-B",
        "store": {"get_status": 404},
        "team": OAUTH_WORKSPACE,
        "warns": f"read failure, status=404); using the default workspace {OAUTH_WORKSPACE}",
    },
]


@pytest.mark.parametrize("row", ROWS, ids=[row["id"] for row in ROWS])
def test_the_pick_lives_in_the_server_record(row: dict[str, Any], caplog) -> None:
    store = RoutingSessionStore(**row["store"])
    harness = _Harness(routing=store)
    if "seed" in row:
        store.rows[(SM_JWT, "mcp:test")] = row["seed"]

    async def first(client):
        session = await harness.open_session(client, OAUTH_BEARER)
        outcomes = []
        for team in row["steps"]:
            outcomes.append(
                await harness.call(
                    client, OAUTH_BEARER, session, "set_team", {"team": f"team-{team}"}
                )
            )
        return session, outcomes

    session, outcomes = anyio.run(_run, harness, first)
    final = harness
    if row["restart"]:
        final = _Harness(
            routing=store
        )  # a new process: new middleware, new exchange cache

    caplog.set_level(logging.WARNING)

    async def probe(client):
        # The restarted server has never seen the old session id: open a new one, as a reconnecting client does.
        probe_session = await final.open_session(client, OAUTH_BEARER)
        assert not row["restart"] or probe_session != session
        return await final.call(client, OAUTH_BEARER, probe_session, "probe")

    result = anyio.run(_run, final, probe)

    assert result.get("isError") is not True
    assert json.loads(_text(result))["team_id"] == row["team"]
    puts = [call for call in store.calls if call[0] == "PUT"]
    assert len(puts) == row.get("puts", 0)
    if row.get("refused"):
        assert outcomes[0]["isError"] is True
        assert store.rows == {}
    if "warns" in row:
        assert any(row["warns"] in record.getMessage() for record in caplog.records)


def test_the_read_is_bearer_only_and_the_write_names_the_client_key() -> None:
    store = RoutingSessionStore()
    harness = _Harness(routing=store)

    async def body(client):
        session = await harness.open_session(client, OAUTH_BEARER)
        await harness.call(
            client, OAUTH_BEARER, session, "set_team", {"team": "team-B"}
        )
        await harness.call(client, OAUTH_BEARER, session, "probe")

    anyio.run(_run, harness, body)

    kinds = [call[0] for call in store.calls]
    # the switch call itself reads first (the middleware runs before the tool), then PUT, then the probe reads
    assert kinds == ["GET", "PUT", "GET"]
    put, get = store.calls[1][1], store.calls[2][1]
    assert put["body"] == {"workspace_id": "team-B", "client_key": "mcp:test"}
    assert get["client_key"] == "mcp:test"
    for call in (put, get):
        assert call["headers"]["authorization"].startswith("Bearer ")
        assert "x-workspace-id" not in call["headers"]


def test_no_per_process_session_dict_is_left() -> None:
    harness = _Harness()

    assert not hasattr(harness.middleware, "_session_teams")
    assert not hasattr(HostedIdentityMiddleware, "_session_teams")


def test_a_refused_switch_is_surfaced_by_the_hosted_tool_and_changes_nothing(
    monkeypatch,
) -> None:
    """The server says 403 even though /memory/teams listed the workspace (membership changed meanwhile)."""
    harness = _ServerHarness()
    harness.routing.forbidden.add("team-B")
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        switched = await harness.call(
            client, session_id, "switch_team", {"team_id": "team-B"}
        )
        who = await harness.call(client, session_id, "whoami")
        return switched, who

    switched, who = harness.run(body)

    assert switched["isError"] is True
    assert "you are not a member of that workspace" in _text(switched)
    assert "team-personal" in _text(who)
    assert harness.routing.rows == {}


# A caller removed from workspace REV whose saved pick still names it (the routing record outlives the membership).
# `check` picks the assertion: the switch to a still-member workspace succeeds and saves the new pick (d), and the
# listing that switch_team preflights carries no stale X-Workspace-Id (e).
REVOKED_ROWS: list[dict[str, str]] = [
    {"id": "d-switch-away-from-revoked-pick-succeeds", "check": "switched"},
    {"id": "e-team-listing-omits-the-stale-header", "check": "no-stale-header"},
]


@pytest.mark.parametrize("row", REVOKED_ROWS, ids=[row["id"] for row in REVOKED_ROWS])
def test_a_revoked_pick_does_not_block_switch_team(
    row: dict[str, str], monkeypatch
) -> None:
    harness = _ServerHarness()
    harness.routing.rows[(API_KEY, "mcp:test")] = "team-REV"
    team_headers: list[dict[str, str]] = []
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request",
        fake_svc_api(revoked=("team-REV",), team_headers=team_headers),
    )

    async def body(client):
        session_id = await harness.session(client)
        switched = await harness.call(
            client, session_id, "switch_team", {"team_id": "team-B"}
        )
        who = await harness.call(client, session_id, "whoami")
        return switched, who

    switched, who = harness.run(body)

    if row["check"] == "switched":
        assert switched.get("isError") is not True, _text(switched)
        puts = [call for call in harness.routing.calls if call[0] == "PUT"]
        assert [put[1]["body"]["workspace_id"] for put in puts] == ["team-B"]
        assert harness.routing.rows[(API_KEY, "mcp:test")] == "team-B"
        assert "team-B" in _text(who)
    else:
        assert team_headers
        assert all("x-workspace-id" not in sent for sent in team_headers)
