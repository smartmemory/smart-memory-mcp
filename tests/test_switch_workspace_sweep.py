"""MCP-SWITCH-WORKSPACE-NAMING-SWEEP-1 items 5 and 8, one row table.

Item 5: a Team id is not a workspace id. The switch (hosted and stdio) and the hosted
`list_workspaces` read only `GET /memory/workspaces`; the fakes here also serve the
caller's Teams (`GET /memory/teams`) with a Team that is no workspace (`team-ops`) and
omit an independent workspace shared to the caller (`ws-shared`), so a listing that
reads Teams again (the pre-WS-ROUTING-5 `_list_team_rows`) fails rows here.

Item 8: after a migrate the next write goes to the workspace the caller switched to,
or the reply and a WARNING name the fallback. Never a silent move to another one.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
from typing import Any

import httpx
import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import portability_tools

from ._tools import tool_fn
from .test_hosted_tools_allowlist import _Harness, _text, fake_svc_api
from .test_switch_workspace import (  # noqa: F401  (_isolate is an autouse fixture)
    API,
    FakeService,
    _auth_me,
    _isolate,
    _remote,
    _warnings,
)

# The caller's Teams: `team-ops` is a Team and no workspace.
TEAMS = [
    {"team_id": "team-personal", "name": "Personal"},
    {"team_id": "team-ops", "name": "Ops"},
]
# The caller's workspaces: `ws-shared` is shared to the caller and no Team of theirs.
HOSTED_WORKSPACES = [
    {"workspace_id": "team-personal", "name": "Personal"},
    {"workspace_id": "ws-shared", "name": "Shared"},
]


def _teams_response(method: str, url: str) -> httpx.Response | None:
    if httpx.URL(url).path == "/memory/teams":
        return httpx.Response(
            200, json={"teams": TEAMS}, request=httpx.Request(method, url)
        )
    return None


def _svc_with_teams():
    base = fake_svc_api()

    def handler(method: str, url: str, **kwargs: Any) -> httpx.Response:
        if (teams := _teams_response(method, url)) is not None:
            return teams
        if httpx.URL(url).path == "/memory/workspaces":
            body = {"workspaces": HOSTED_WORKSPACES}
            return httpx.Response(200, json=body, request=httpx.Request(method, url))
        return base(method, url, **kwargs)

    return handler


class _StdioServiceWithTeams(FakeService):
    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if (teams := _teams_response(method, url)) is not None:
            return teams
        return super().__call__(method, url, **kwargs)


# --- runners: each returns (reply, where the next request went, WARNING lines) ------


def _hosted(monkeypatch, caplog, _tmp_path, arg: str):
    harness = _Harness()
    harness.routing.install(monkeypatch, _svc_with_teams())

    async def body(client):
        session_id = await harness.session(client)
        if arg == "<list>":
            return await harness.call(client, session_id, "list_workspaces")
        result = await harness.call(
            client, session_id, "switch_workspace", {"workspace": arg}
        )
        harness.routing.resolved.clear()
        await harness.call(client, session_id, "memory_search", {"query": "q"})
        return result

    reply = _text(harness.run(body))
    lands = harness.routing.resolved[0][2] if harness.routing.resolved else None
    return reply, lands, _warnings(caplog)


def _stdio(monkeypatch, caplog, _tmp_path, arg: str):
    service = _StdioServiceWithTeams()
    _remote(monkeypatch, service)
    reply = tool_fn("switch_workspace")(workspace=arg)
    mark = len(service.seen)
    tool_fn("memory_get")(item_id="mem-1")
    return reply, service.headers_after(mark)[0], _warnings(caplog)


# Migrate scenarios: (switch before the migrate, switch during the export, targets).
# Each import succeeds. Data exported from the pre-export workspace.
MIGRATES = {
    "pick-then-round-trip": ("Beta", None, ["local", "remote"]),
    "switch-during-export-then-local": ("Beta", "Alpha", ["local"]),
    "switch-during-export-no-pick-then-local": (None, "Alpha", ["local"]),
    "switch-during-export-then-round-trip": ("Beta", "Alpha", ["local", "remote"]),
}


def _migrate(monkeypatch, caplog, tmp_path, arg: str):
    pick, export_switch, targets = MIGRATES[arg]
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", "remote")
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test")
    monkeypatch.setenv("SMARTMEMORY_API_URL", API)
    for name in ("SMARTMEMORY_WORKSPACE_ID", "SMARTMEMORY_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(portability_tools._parked, "remote_workspace", None)
    cfg = types.SimpleNamespace(mode="remote", team_id="", api_url=API)
    config_module = types.ModuleType("smartmemory_app.config")
    config_module.load_config = lambda: cfg
    config_module.save_config = lambda current: None
    monkeypatch.setitem(
        sys.modules, "smartmemory_app", types.ModuleType("smartmemory_app")
    )
    monkeypatch.setitem(sys.modules, "smartmemory_app.config", config_module)
    monkeypatch.setattr(LocalBackend, "add", lambda self, content, **kw: "m-1")

    service = FakeService()
    _remote(monkeypatch, service)
    if pick:
        assert "Switched to workspace" in tool_fn("switch_workspace")(workspace=pick)

    def fake_export(backend, path):
        if export_switch and not isinstance(backend, LocalBackend):
            # A parallel tool call (FastMCP runs sync tools in a threadpool).
            reply = tool_fn("switch_workspace")(workspace=export_switch)
            assert "Switched to workspace" in reply, reply
        return 3

    monkeypatch.setattr(portability_tools, "_export_okf", fake_export)
    monkeypatch.setattr(
        portability_tools, "_import_okf", lambda backend, path: {"imported": 3}
    )
    caplog.set_level(logging.WARNING)
    for target in targets:
        reply = tool_fn("memory_migrate")(target=target)
    mark = len(service.seen)
    tool_fn("memory_add")(content="after the migrate")
    headers = service.headers_after(mark)
    return reply, (headers[0] if headers else "local"), _warnings(caplog)


RUNNERS = {"hosted": _hosted, "stdio": _stdio, "migrate": _migrate}

# runner, argument, reply contains, reply lacks, where the next request goes (hosted: the workspace svc-api routed
# it to; stdio/migrate: its X-Workspace-Id, None = no pick header, "local" = the write went to Lite), a WARNING
# line contains ("" = no WARNING check).
ROWS = [
    # item 5, hosted: a workspace shared to the caller is selectable by id and name
    (
        "hosted",
        "ws-shared",
        ("Switched to workspace: Shared (ws-shared)",),
        (),
        "ws-shared",
        "",
    ),
    (
        "hosted",
        "shared",
        ("Switched to workspace: Shared (ws-shared)",),
        (),
        "ws-shared",
        "",
    ),
    # item 5, hosted: a Team id (or Team name) is never accepted as a workspace
    (
        "hosted",
        "team-ops",
        ("Workspace not switched", "You are not a member of workspace 'team-ops'"),
        ("Switched",),
        "team-personal",
        "Workspace switch refused",
    ),
    (
        "hosted",
        "Ops",
        ("Workspace not switched",),
        ("Switched",),
        "team-personal",
        "Workspace switch refused",
    ),
    # item 5, hosted: a non-member workspace is refused
    (
        "hosted",
        "ws-nobody",
        ("Workspace not switched",),
        ("Switched",),
        "team-personal",
        "Workspace switch refused",
    ),
    # item 5, hosted: the listing offers workspaces only
    (
        "hosted",
        "<list>",
        ("Shared (ws-shared)", "Personal (team-personal)"),
        ("team-ops", "Ops"),
        None,
        "",
    ),
    # item 5, stdio: the same split
    ("stdio", "Beta", ("Switched to workspace: Beta (ws-b)",), (), "ws-b", ""),
    (
        "stdio",
        "team-ops",
        ("Workspace not switched",),
        ("Switched",),
        None,
        "Workspace switch refused",
    ),
    # item 8: a switch, then a successful round trip, comes back to the switched workspace
    (
        "migrate",
        "pick-then-round-trip",
        ("Imported into workspace: ws-b (your earlier remote workspace).",),
        (),
        "ws-b",
        "",
    ),
    # item 8: a switch made during the export of a successful move to local is named, not dropped silently
    (
        "migrate",
        "switch-during-export-then-local",
        (
            "Remote workspace ws-b is no longer active",
            "Your switch to ws-a during the export was not kept",
        ),
        (),
        "local",
        "switch_workspace to ws-a during the export was not kept",
    ),
    (
        "migrate",
        "switch-during-export-no-pick-then-local",
        (
            "Workspace is now default (local mode).",
            "Your switch to ws-a during the export was not kept",
        ),
        (),
        "local",
        "switch_workspace to ws-a during the export was not kept",
    ),
    # item 8: the round trip returns the data to where it came from (ws-b), which the move to local named
    (
        "migrate",
        "switch-during-export-then-round-trip",
        ("Imported into workspace: ws-b (your earlier remote workspace).",),
        (),
        "ws-b",
        "switch_workspace to ws-a during the export was not kept",
    ),
]


@pytest.mark.parametrize(
    ("runner", "arg", "reply_has", "reply_lacks", "lands", "warns"),
    ROWS,
    ids=[f"{row[0]}-{row[1]}" for row in ROWS],
)
def test_workspace_ids_and_switch_survival(
    monkeypatch, caplog, tmp_path, runner, arg, reply_has, reply_lacks, lands, warns
) -> None:
    with caplog.at_level(logging.WARNING):
        reply, landed, warnings = RUNNERS[runner](monkeypatch, caplog, tmp_path, arg)

    for expected in reply_has:
        assert expected in reply, reply
    for absent in reply_lacks:
        assert absent not in reply, reply
    assert landed == lands
    if warns:
        assert any(warns in w for w in warnings), warnings
