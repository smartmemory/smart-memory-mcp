"""MCP-SWITCH-WORKSPACE-1: `switch_workspace` on every backend, the `switch_team` alias,
`whoami` tenant + workspace, and the no-tenant-parameter schema walk.

One behaviour everywhere: the switch acts on the live session (stdio: the
dispatch-held `RemoteBackend`, `backends/dispatch.py` `_backend`; hosted: the MCP
session), the next request carries the new `X-Workspace-Id`, and a failed switch
leaves the previous workspace active and says so.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import types
from typing import Any

import httpx
import pytest

from smartmemory_mcp import workspace_select
from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.tier import Tier
from smartmemory_mcp.tools import common
from smartmemory_mcp.workspace_select import ALIAS_NOTICE

from ._tools import tool_fn
from .test_hosted_route_contract import _load_snapshot, _served_routes
from .test_hosted_tools_allowlist import ITEM, _Harness, _text, fake_svc_api
from .test_tier_registration import MCP_ROOT, _clean_env

API = "http://svc.test"
WORKSPACES = [
    {"workspace_id": "ws-a", "name": "Alpha", "tenant_id": "tenant-1"},
    {"workspace_id": "ws-b", "name": "Beta", "tenant_id": "tenant-1"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    before = hosted_identity.hosted_mode_enabled()
    hosted_identity.set_hosted_mode(False)
    workspace_select.reset_alias_warning()
    monkeypatch.setattr("smartmemory_mcp.server.resolve_tier", lambda: Tier.FREE)
    try:
        yield
    finally:
        dispatch.reset_backend()
        common.reset_backend()
        hosted_identity.set_hosted_mode(before)


class FakeService:
    """svc-api for the stdio remote backend; records every request's workspace header."""

    def __init__(
        self, *, list_status: int = 200, refuse: frozenset[str] = frozenset()
    ) -> None:
        self.list_status = list_status
        self.refuse = refuse
        self.seen: list[tuple[str, str, str | None]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path
        header = (kwargs.get("headers") or {}).get("X-Workspace-Id")
        self.seen.append((method, path, header))
        request = httpx.Request(method, url)
        if path == "/memory/workspaces":
            if self.list_status != 200:
                return httpx.Response(
                    self.list_status, json={"detail": "down"}, request=request
                )
            return httpx.Response(200, json={"workspaces": WORKSPACES}, request=request)
        if header in self.refuse:
            # What common answers for a workspace the caller does not belong to.
            body = {"detail": {"code": "team_not_in_tenant", "message": "not a member"}}
            return httpx.Response(403, json=body, request=request)
        return httpx.Response(200, json=ITEM, request=request)

    def headers_after(self, index: int) -> list[str | None]:
        return [h for _, path, h in self.seen[index:] if path != "/memory/workspaces"]


def _auth_me(_url: str, **_kwargs: Any) -> httpx.Response:
    body = {"email": "dev@test.com", "tenant_id": "tenant-1", "default_team_id": "ws-a"}
    return httpx.Response(200, json=body, request=httpx.Request("GET", _url))


def _remote(monkeypatch, service: FakeService) -> RemoteBackend:
    """The dispatch-held backend, exactly as `resolve_backend()` hands it to every tool."""
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", service)
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.get", _auth_me)
    backend = RemoteBackend(api_url=API, api_key="sk_test", team_id="")
    # Both caches hold the one process backend, as after the first tool call.
    monkeypatch.setattr(dispatch, "_backend", backend)
    monkeypatch.setattr(common, "_backend", backend)
    return backend


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- 1. one table: backend x {name, id, unknown} ------------------------------------


def _run_remote(monkeypatch, arg: str) -> tuple[str, bool, str | None]:
    service = FakeService()
    backend = _remote(monkeypatch, service)
    assert backend.active_workspace_id == "ws-a"  # discovered default
    text = tool_fn("switch_workspace")(workspace=arg)
    mark = len(service.seen)
    tool_fn("memory_get")(item_id="mem-1")  # the next request, through a real tool
    return (
        text,
        text.startswith("Workspace not switched"),
        service.headers_after(mark)[0],
    )


def _run_hosted(monkeypatch, arg: str) -> tuple[str, bool, str | None]:
    headers: list[str] = []

    def spy(method, url, **kwargs):
        headers.append((kwargs.get("headers") or {}).get("X-Workspace-Id", ""))
        return fake_svc_api()(method, url, **kwargs)

    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", spy)

    async def body(client):
        session_id = await harness.session(client)
        result = await harness.call(
            client, session_id, "switch_workspace", {"workspace": arg}
        )
        headers.clear()
        await harness.call(client, session_id, "memory_search", {"query": "q"})
        return result

    result = harness.run(body)
    return _text(result), result.get("isError") is True, headers[0]


def _run_local(_monkeypatch, arg: str) -> tuple[str, bool, str | None]:
    result = LocalBackend().switch_workspace(arg)
    return result.message, not result.ok, None


RUNNERS = {"remote": _run_remote, "hosted": _run_hosted, "local": _run_local}

# backend, argument, switched?, workspace the NEXT request carries (None: no header)
SWITCH_ROWS = [
    ("remote", "Beta", True, "ws-b"),  # by name, case-insensitive match below
    ("remote", "ws-b", True, "ws-b"),  # by id
    ("remote", "nobody", False, "ws-a"),  # unknown: previous stays
    ("hosted", "team-B", True, "team-B"),  # by id
    ("hosted", "personal", True, "team-personal"),  # by name ("Personal")
    ("hosted", "someone-elses-team", False, "team-personal"),
    ("local", "default", True, None),
    ("local", "ws-b", False, None),  # Lite has no workspaces
]


@pytest.mark.parametrize(("backend", "arg", "switched", "next_header"), SWITCH_ROWS)
def test_switch_workspace_on_every_backend(
    monkeypatch, caplog, backend, arg, switched, next_header
) -> None:
    with caplog.at_level(logging.WARNING):
        text, failed, header = RUNNERS[backend](monkeypatch, arg)

    assert "restart" not in text.lower()
    assert header == next_header
    assert failed is (not switched), text
    if switched:
        assert not any("Workspace switch refused" in m for m in _warnings(caplog))
        assert (
            "Already in workspace" in text
            if backend == "local"
            else "Switched to workspace" in text
        )
    else:
        assert "Workspace not switched" in text
        assert "Still using workspace:" in text
        assert any("Workspace switch refused" in m for m in _warnings(caplog))


# --- 2. stdio remote: the service is still the membership gate --------------------


def test_a_service_refusal_after_the_switch_is_an_error_not_a_fallback(
    monkeypatch,
) -> None:
    """Listed but refused by svc-api (e.g. membership revoked): the call errors with
    the new workspace named, and nothing retries under the old one."""
    service = FakeService(refuse=frozenset({"ws-b"}))
    backend = _remote(monkeypatch, service)
    assert "Switched to workspace: Beta (ws-b)" in tool_fn("switch_workspace")(
        workspace="ws-b"
    )
    mark = len(service.seen)

    with pytest.raises(RuntimeError, match="API error 403"):
        tool_fn("memory_get")(item_id="mem-1")

    assert service.headers_after(mark) == ["ws-b"]
    assert backend.active_workspace_id == "ws-b"


def test_an_unanswered_workspace_list_keeps_the_previous_workspace(
    monkeypatch, caplog
) -> None:
    service = FakeService(list_status=503)
    backend = _remote(monkeypatch, service)
    with caplog.at_level(logging.WARNING):
        text = tool_fn("switch_workspace")(workspace="ws-b")

    assert text.startswith("Workspace not switched: could not list your workspaces")
    assert "Still using workspace: ws-a" in text
    assert backend.active_workspace_id == "ws-a"
    assert any("Workspace lookup failed" in m for m in _warnings(caplog))
    # The listing never carries the (possibly stale) current workspace header.
    assert [h for _, p, h in service.seen if p == "/memory/workspaces"] == [None]


def test_save_as_default_writes_the_config_only_when_asked(monkeypatch) -> None:
    saved: list[str] = []
    cfg = types.SimpleNamespace(team_id="")
    fake = types.ModuleType("smartmemory_app.config")
    fake.load_config = lambda: cfg
    fake.save_config = lambda c: saved.append(c.team_id)
    monkeypatch.setitem(sys.modules, "smartmemory_app.config", fake)
    _remote(monkeypatch, FakeService())

    assert "Saved as the default" not in tool_fn("switch_workspace")(workspace="ws-b")
    assert saved == []
    text = tool_fn("switch_workspace")(workspace="Alpha", save_as_default=True)
    assert "Saved as the default for new sessions." in text
    assert saved == ["ws-a"]


# --- 2b. every process-backend rebuild after a live switch (review H1) ---------------

# One row per `memory_migrate` rebuild site. Remote rows start on the configured
# default ws-a and switch live (unsaved) to Beta (ws-b); local rows start in Lite.
# Each step is (target, import outcome). After the steps: the last message, where
# every import landed, and the X-Workspace-Id of the next `memory_add` ([] = no
# remote request, the write went to Lite).
REBUILD_ROWS = [
    pytest.param(
        "remote",
        [("local", "ok")],
        ["Workspace changed: now default", "Remote workspace ws-b is no longer active"],
        ["local"],
        [],
        True,
        id="forward-remote-to-local-says-ws-b-left",
    ),
    pytest.param(
        "remote",
        [("local", "all-fail")],
        ["Config reverted to 'remote'", "Still on workspace: ws-b."],
        ["local"],
        ["ws-b"],
        False,
        id="total-failure-revert-keeps-ws-b",
    ),
    pytest.param(
        "remote",
        [("local", "raise")],
        ["Config reverted to 'remote'", "Still on workspace: ws-b."],
        ["local"],
        ["ws-b"],
        False,
        id="exception-revert-keeps-ws-b",
    ),
    pytest.param(
        "remote",
        [("local", "ok"), ("remote", "ok")],
        ["Imported into workspace: ws-b (your earlier remote workspace)."],
        ["local", "ws-b"],
        ["ws-b"],
        True,
        id="round-trip-returns-to-ws-b",
    ),
    pytest.param(
        "remote",
        [("local", "ok"), ("remote", "raise")],
        ["Config reverted to 'local'", "Still on workspace: default (local mode)."],
        ["local", "ws-b"],
        [],
        True,
        id="round-trip-exception-revert-stays-local",
    ),
    pytest.param(
        "remote",
        [("local", "ok"), ("remote", "all-fail"), ("remote", "ok")],
        ["Imported into workspace: ws-b (your earlier remote workspace)."],
        ["local", "ws-b", "ws-b"],
        ["ws-b"],
        True,
        id="round-trip-after-failed-return-still-ws-b",
    ),
    pytest.param(
        "local",
        [("remote", "ok")],
        ["Imported into workspace: ws-a."],
        ["ws-a"],
        ["ws-a"],
        False,
        id="forward-local-to-remote-names-config-default",
    ),
]


@pytest.mark.parametrize(
    ("start", "steps", "message_has", "imported_into", "next_header", "warns_ws_b"),
    REBUILD_ROWS,
)
def test_a_backend_rebuild_after_a_live_switch_never_drops_it_silently(
    monkeypatch,
    caplog,
    tmp_path,
    start,
    steps,
    message_has,
    imported_into,
    next_header,
    warns_ws_b,
) -> None:
    import tempfile

    from smartmemory_mcp.tools import portability_tools

    # The failure paths keep their temp bundle; keep it under tmp_path, not $TMPDIR.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", start)
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test")
    monkeypatch.setenv("SMARTMEMORY_API_URL", API)
    monkeypatch.setitem(portability_tools._parked, "remote_workspace", None)
    cfg = types.SimpleNamespace(mode=start, team_id="ws-a", api_url=API)
    config_module = types.ModuleType("smartmemory_app.config")
    config_module.load_config = lambda: cfg
    config_module.save_config = lambda current: None
    monkeypatch.setitem(
        sys.modules, "smartmemory_app", types.ModuleType("smartmemory_app")
    )
    monkeypatch.setitem(sys.modules, "smartmemory_app.config", config_module)
    local_writes: list[str] = []
    monkeypatch.setattr(
        LocalBackend,
        "add",
        lambda self, content, **kw: local_writes.append(content) or "m-1",
    )

    service = FakeService()
    if start == "remote":
        _remote(monkeypatch, service)
        assert "Switched to workspace: Beta (ws-b)" in tool_fn("switch_workspace")(
            workspace="Beta"
        )
    else:
        monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", service)
        monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.get", _auth_me)

    landed: list[str] = []
    outcome = {"now": "ok"}

    def fake_import(backend, path):
        landed.append(
            "local"
            if isinstance(backend, LocalBackend)
            else backend.active_workspace_id
        )
        if outcome["now"] == "raise":
            raise RuntimeError("import failed")
        return (
            {"imported": 0, "failed": 3}
            if outcome["now"] == "all-fail"
            else {"imported": 3}
        )

    monkeypatch.setattr(portability_tools, "_export_okf", lambda backend, path: 3)
    monkeypatch.setattr(portability_tools, "_import_okf", fake_import)
    caplog.set_level(logging.WARNING)
    for target, result in steps:
        outcome["now"] = result
        message = tool_fn("memory_migrate")(target=target)

    for expected in message_has:
        assert expected in message, message
    assert landed == imported_into
    mark = len(service.seen)
    tool_fn("memory_add")(content="note meant for Beta")
    assert service.headers_after(mark) == next_header
    assert local_writes == ([] if next_header else ["note meant for Beta"])
    dropped = [w for w in _warnings(caplog) if "memory_migrate" in w and "ws-b" in w]
    assert bool(dropped) is warns_ws_b, _warnings(caplog)


def test_the_stdio_workspace_list_route_and_fields_are_served() -> None:
    """`RemoteBackend.list_workspaces` reads `GET /memory/workspaces` -> `workspaces[]`
    with `workspace_id` and `name`; pinned to the service's OpenAPI snapshot."""
    assert ("get", "/memory/workspaces") in _served_routes()
    schemas = _load_snapshot()["components"]["schemas"]
    assert "workspaces" in schemas["WorkspaceList"]["properties"]
    assert {"workspace_id", "name"} <= set(schemas["WorkspaceSummary"]["required"])


# --- 3. the deprecated alias --------------------------------------------------------


def _alias_stdio(monkeypatch) -> list[str]:
    _remote(monkeypatch, FakeService())
    return [
        tool_fn("switch_team")(team_id="ws-b"),
        tool_fn("switch_team")(team_id="Alpha"),
    ]


def _alias_hosted(monkeypatch) -> list[str]:
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        return [
            _text(await harness.call(client, session_id, "switch_team", {"team_id": t}))
            for t in ("team-B", "team-personal")
        ]

    return harness.run(body)


@pytest.mark.parametrize("server", ["stdio", "hosted"])
def test_switch_team_is_the_same_switch_with_one_warning(
    monkeypatch, caplog, server
) -> None:
    run = _alias_stdio if server == "stdio" else _alias_hosted
    with caplog.at_level(logging.WARNING):
        outputs = run(monkeypatch)

    for text in outputs:
        assert text.startswith(ALIAS_NOTICE)
        assert "Switched to workspace" in text
        assert "restart" not in text.lower()
    assert sum(ALIAS_NOTICE in m for m in _warnings(caplog)) == 1


# --- 4. whoami: tenant + workspace, never "team" ------------------------------------


def _whoami_remote(monkeypatch) -> str:
    _remote(monkeypatch, FakeService())
    return tool_fn("whoami")()


def _whoami_local(monkeypatch) -> str:
    backend = LocalBackend()
    monkeypatch.setattr(dispatch, "_backend", backend)
    monkeypatch.setattr(common, "_backend", backend)
    return tool_fn("whoami")()


def _whoami_hosted(monkeypatch) -> str:
    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_svc_api())

    async def body(client):
        return _text(
            await harness.call(client, await harness.session(client), "whoami")
        )

    return harness.run(body)


WHOAMI_ROWS = [
    (_whoami_remote, ("Tenant: tenant-1", "Workspace: ws-a", "Backend: remote")),
    (_whoami_local, ("Tenant: none", "Workspace: default", "Backend: local")),
    (
        _whoami_hosted,
        ("Tenant: tenant-1", "Workspace: team-personal", "Backend: hosted"),
    ),
]


@pytest.mark.parametrize(
    ("run", "expected"), WHOAMI_ROWS, ids=["remote", "local", "hosted"]
)
def test_whoami_names_tenant_and_workspace(monkeypatch, run, expected) -> None:
    text = run(monkeypatch)
    for fragment in expected:
        assert fragment in text, text
    # The fixture's hosted workspace id is literally "team-personal"; the label is what counts.
    assert "team" not in text.lower().replace("team-personal", "")


def test_login_reports_the_workspace(monkeypatch) -> None:
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.get", _auth_me)
    text = RemoteBackend(api_url=API, api_key="").login("sk_test")
    assert text == "Logged in as dev@test.com, workspace: ws-a"


# --- 5. the listing: no tenant selector, "team" only on the alias -------------------

_LISTING_SNIPPET = """\
import asyncio, json, os
import smartmemory_mcp.tier as tier
import smartmemory_mcp.capabilities as capabilities
from smartmemory_mcp.backends.remote import RemoteBackend
tier.get_api_key = lambda: os.environ.get("SMARTMEMORY_API_KEY", "")
capabilities.get_backend = lambda: RemoteBackend(api_key="sk_test_key_123")
from smartmemory_mcp.server import mcp
tools = asyncio.run(mcp.list_tools())
print(json.dumps([
    {"name": t.name, "title": t.title, "description": t.description, "schema": t.parameters}
    for t in tools
]))
"""


def _stdio_listing() -> list[dict[str, Any]]:
    env = _clean_env(
        SMARTMEMORY_API_KEY="sk_test_key_123", SMARTMEMORY_MCP_FULL_TOOLS="true"
    )
    run = subprocess.run(
        [sys.executable, "-c", _LISTING_SNIPPET],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=MCP_ROOT,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def _hosted_listing() -> list[dict[str, Any]]:
    from fastmcp import Client

    from smartmemory_mcp.hosted.exchange import ExchangeCache
    from smartmemory_mcp.hosted.server import build_hosted_server

    from ._hosted_fixtures import hosted_config, memory_store

    server = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    async def listing():
        async with Client(server) as client:
            return await client.list_tools()

    import anyio

    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "schema": t.inputSchema,
        }
        for t in anyio.run(listing)
    ]


def _property_names(schema: Any) -> set[str]:
    """Every property name anywhere in a JSON schema (nested objects and $defs too)."""
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in (schema.get("properties") or {}).items():
            names.add(key)
            names |= _property_names(value)
        for key, value in schema.items():
            if key != "properties":
                names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


@pytest.mark.parametrize(
    ("server", "listing", "count"),
    [("stdio", _stdio_listing, 100), ("hosted", _hosted_listing, 28)],
)
def test_the_listing_has_no_tenant_selector_and_team_only_on_the_alias(
    server, listing, count
) -> None:
    tools = listing()
    names = {t["name"] for t in tools}

    assert len(tools) == count
    assert {"switch_workspace", "switch_team", "whoami"} <= names
    assert not {n for n in names if "tenant" in n}
    for tool in tools:
        assert not {"tenant", "tenant_id"} & _property_names(tool["schema"]), tool[
            "name"
        ]
    with_team = {
        t["name"]
        for t in tools
        if "team" in json.dumps([t["title"], t["description"], t["schema"]]).lower()
    }
    assert with_team == {"switch_team"}
    alias = next(t for t in tools if t["name"] == "switch_team")
    assert "deprecated" in (alias["title"] + alias["description"]).lower()
    switch = next(t for t in tools if t["name"] == "switch_workspace")
    assert "workspace" in switch["schema"]["properties"]
