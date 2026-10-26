"""WS-ROUTING-5 header rule: `X-Workspace-Id` only for an explicit pick, `X-SM-Client` always.

Contract `ws-routing-contract.json` `headers.X-Workspace-Id`: a tool `workspace_id` argument and a person-written
env/config value are picks; re-sending the `/auth/me` default or the session's workspace is not. A hosted switch is
applied by svc-api's session lever from `X-SM-Client`, which is also what keeps it alive on use (the fold-in row).

Every svc-api answer here comes from `RoutingSessionStore`, the resolver fake (`tests/_hosted_fixtures.py`): call header,
then the session row (24 h idle TTL, refreshed by a session-routed write at most once per hour), then the floor or the
400 `missing_team_context` refusal, only on routes where the real service resolves (`MCP_ROUTE_POLICY`). The fake's
default is the TARGET behaviour of WS-ROUTING-5-PRE-1 (not landed at fix round 1); `pre_landed=False` rows pin today's
service. The MCP never answers a refusal with a guessed workspace (fix round 1). No test reaches a network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends import remote as remote_module
from smartmemory_mcp.backends.remote import MISSING_WORKSPACE_HELP, RemoteBackend
from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.tools import common

from ._hosted_fixtures import RoutingSessionStore
from ._tools import tool_fn
from .test_hosted_tools_allowlist import _Harness, _text, fake_svc_api

API = "http://svc.test"
DEFAULT = "ws-default"  # what /auth/me suggests: display only


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    before = hosted_identity.hosted_mode_enabled()
    hosted_identity.set_hosted_mode(False)
    for name in ("SMARTMEMORY_WORKSPACE_ID", "SMARTMEMORY_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        dispatch.reset_backend()
        common.reset_backend()
        hosted_identity.set_hosted_mode(before)


def _ok(method: str, url: str, **_kwargs: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={"item_id": "mem-1", "status": "ingested"},
        request=httpx.Request(method, url),
    )


class _Spy:
    """Wraps a handler and records the headers of every data-route call that reaches svc-api."""

    def __init__(self, inner) -> None:
        self.inner, self.sent = inner, []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path
        if not path.startswith("/memory/routing/"):
            self.sent.append(dict(kwargs.get("headers") or {}))
        return self.inner(method, url, **kwargs)


def _wire(monkeypatch) -> list[dict[str, str]]:
    """Headers of every data-route attempt as the MCP sent it, refused ones included (call after `install`).

    `httpx.stream` (OKF export) is answered by the same installed handler, so no attempt can leave the process.
    """
    from contextlib import contextmanager

    sent: list[dict[str, str]] = []
    inner = remote_module.httpx.request

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not httpx.URL(url).path.startswith("/memory/routing/"):
            sent.append(dict(kwargs.get("headers") or {}))
        return inner(method, url, **kwargs)

    @contextmanager
    def stream(method: str, url: str, **kwargs: Any):
        yield request(method, url, **kwargs)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", request)
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.stream", stream)
    return sent


def _stdio(
    monkeypatch, store: RoutingSessionStore, team_id: str = ""
) -> tuple[RemoteBackend, _Spy]:
    """The stdio remote backend as dispatch hands it to every tool, its svc-api answered by `store`."""
    spy = _Spy(_ok)
    store.install(monkeypatch, spy)
    backend = RemoteBackend(api_url=API, api_key="sk_test", team_id=team_id)
    backend._session.update({"_bootstrapped": True, "default_team_id": DEFAULT})
    monkeypatch.setattr(dispatch, "_backend", backend)
    monkeypatch.setattr(common, "_backend", backend)
    return backend, spy


# --- 1. no pick sends no header ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NoPickRow:
    id: str
    hosted: bool
    personal_floor: bool
    account_default: (
        str | None
    )  # the service's flag-off floor (WS-ROUTING-5-PRE-1 target)
    header: str | None  # X-Workspace-Id the write carried
    routed: tuple[str, str]  # (workspace, routed_by) svc-api gave it


NO_PICK_ROWS = [
    NoPickRow(
        "stdio-floor-on",
        False,
        True,
        None,
        None,
        ("ws_personal_user-1", "account_default"),
    ),
    NoPickRow(
        "hosted-floor-on",
        True,
        True,
        None,
        None,
        ("ws_personal_user-1", "account_default"),
    ),
    # Floor off: the SERVICE floors to the account default (PRE target); the MCP still sends no header.
    NoPickRow(
        "stdio-floor-off-service-default",
        False,
        False,
        DEFAULT,
        None,
        (DEFAULT, "account_default"),
    ),
    NoPickRow(
        "hosted-floor-off-service-default",
        True,
        False,
        "team-personal",
        None,
        ("team-personal", "account_default"),
    ),
]


@pytest.mark.parametrize("row", NO_PICK_ROWS, ids=[r.id for r in NO_PICK_ROWS])
def test_no_pick_sends_no_header(monkeypatch, row: NoPickRow) -> None:
    if row.hosted:
        harness = _Harness()
        harness.routing.personal_floor = row.personal_floor
        harness.routing.default_team_id = row.account_default
        spy = _Spy(fake_svc_api())
        harness.routing.install(monkeypatch, spy)

        async def body(client):
            session_id = await harness.session(client)
            return await harness.call(
                client, session_id, "memory_add", {"content": "x"}
            )

        result = harness.run(body)
        assert result.get("isError") is not True, _text(result)
        store, client_key = harness.routing, "mcp:test"
    else:
        store = RoutingSessionStore(
            personal_floor=row.personal_floor, default_team_id=row.account_default
        )
        _, spy = _stdio(monkeypatch, store)
        tool_fn("memory_add")(content="x")
        client_key = "mcp:stdio"

    writes = [h for h in spy.sent if h.get("X-SM-Client")]
    assert [h.get("X-Workspace-Id") for h in writes] == [row.header]
    assert [h["X-SM-Client"] for h in writes] == [client_key]
    assert [r[2:] for r in store.resolved] == [row.routed]


# --- 2. a tool or backend workspace_id argument is a pick ---------------------------------------------------------------


@dataclass(frozen=True)
class ArgumentRow:
    id: str
    tool: str
    arguments: dict[str, Any]
    hosted: bool
    header: str | None  # None: no write reached svc-api
    error: str | None  # substring of the refusal


ARGUMENT_ROWS = [
    ArgumentRow(
        "stdio-memory_add",
        "memory_add",
        {"content": "x", "workspace_id": "ws-arg"},
        False,
        "ws-arg",
        None,
    ),
    ArgumentRow(
        "stdio-memory_ingest",
        "memory_ingest",
        {"content": "x", "workspace_id": "ws-arg"},
        False,
        "ws-arg",
        None,
    ),
    ArgumentRow(
        "stdio-blank-is-no-pick",
        "memory_add",
        {"content": "x", "workspace_id": "  "},
        False,
        None,
        None,
    ),
    ArgumentRow(
        "hosted-memory_add",
        "memory_add",
        {"content": "x", "workspace_id": "ws-arg"},
        True,
        "ws-arg",
        None,
    ),
    ArgumentRow(
        "hosted-memory_ingest",
        "memory_ingest",
        {"content": "x", "workspace_id": "ws-arg"},
        True,
        "ws-arg",
        None,
    ),
    ArgumentRow(
        "lite-refuses",
        "memory_add",
        {"content": "x", "workspace_id": "ws-arg"},
        False,
        None,
        "has no workspaces",
    ),
]


@pytest.mark.parametrize("row", ARGUMENT_ROWS, ids=[r.id for r in ARGUMENT_ROWS])
def test_a_tool_workspace_argument_is_sent_as_the_header(
    monkeypatch, row: ArgumentRow
) -> None:
    if row.hosted:
        harness = _Harness()
        spy = _Spy(fake_svc_api())
        harness.routing.install(monkeypatch, spy)

        async def body(client):
            session_id = await harness.session(client)
            await harness.call(
                client, session_id, "switch_workspace", {"workspace": "team-B"}
            )
            spy.sent.clear()
            harness.routing.resolved.clear()
            return await harness.call(client, session_id, row.tool, row.arguments)

        result = harness.run(body)
        assert result.get("isError") is not True, _text(result)
        # The argument beats the session pick for this call only, and the result line names it.
        assert f"Workspace: {row.header}, routed by call." in _text(result)
        assert harness.routing.resolved[-1][2:] == (row.header, "call")
        sent = spy.sent
    elif row.error:
        lite = LocalBackend.__new__(LocalBackend)
        monkeypatch.setattr(dispatch, "_backend", lite)
        monkeypatch.setattr(common, "_backend", lite)
        with pytest.raises(ValueError, match=row.error):
            tool_fn(row.tool)(**row.arguments)
        return
    else:
        store = RoutingSessionStore(personal_floor=True)
        _, spy = _stdio(monkeypatch, store)
        tool_fn(row.tool)(**row.arguments)
        sent = spy.sent

    assert [h.get("X-Workspace-Id") for h in sent] == [row.header]


# --- 3. a person-written env or config value is a pick ------------------------------------------------------------------

# (SMARTMEMORY_WORKSPACE_ID, SMARTMEMORY_TEAM_ID, constructor team = the saved config / a carried live switch) -> header.
ENV_PICK_ROWS = [
    pytest.param("ws-env", "", "", "ws-env", id="workspace-env"),
    pytest.param("", "team-legacy", "", "team-legacy", id="legacy-team-env"),
    pytest.param("", "", "ws-config", "ws-config", id="config-team"),
    pytest.param("", "", "", None, id="nothing-written"),
]


@pytest.mark.parametrize(
    ("workspace_env", "team_env", "configured", "header"), ENV_PICK_ROWS
)
def test_an_env_or_config_pick_is_sent_as_the_header(
    monkeypatch, workspace_env: str, team_env: str, configured: str, header: str | None
) -> None:
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", workspace_env)
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", team_env)
    monkeypatch.setattr("smartmemory_mcp.backends.remote._warned_env_conflict", False)
    store = RoutingSessionStore(personal_floor=True)
    _, spy = _stdio(monkeypatch, store, team_id=configured)

    tool_fn("memory_add")(content="x")

    assert [h.get("X-Workspace-Id") for h in spy.sent] == [header]
    assert [h["X-SM-Client"] for h in spy.sent] == ["mcp:stdio"]


# --- 4. after a hosted switch, a write is routed by the session -----------------------------------------------------------

# switch target -> (workspace, routed_by) of the next write, and the header it carried.
SWITCH_ROWS = [
    pytest.param("team-B", ("team-B", "session"), None, id="switched"),
    pytest.param(
        "team-personal",
        ("team-personal", "session"),
        None,
        id="switched-to-the-default",
    ),
]


@pytest.mark.parametrize(("target", "routed", "header"), SWITCH_ROWS)
def test_a_hosted_write_after_a_switch_is_routed_by_the_session(
    monkeypatch, target: str, routed: tuple[str, str], header: str | None
) -> None:
    harness = _Harness()
    spy = _Spy(fake_svc_api())
    harness.routing.install(monkeypatch, spy)

    async def body(client):
        session_id = await harness.session(client)
        await harness.call(
            client, session_id, "switch_workspace", {"workspace": target}
        )
        spy.sent.clear()
        harness.routing.resolved.clear()
        return await harness.call(client, session_id, "memory_add", {"content": "x"})

    result = harness.run(body)

    assert [h.get("X-Workspace-Id") for h in spy.sent] == [header]
    assert harness.routing.resolved == [("POST", "/memory/add", *routed)]
    assert f"routed by {routed[1]}." in _text(result)


# --- 5. a refusal is a clear error and is never retried with a guessed workspace ----------------------------------------


@dataclass(frozen=True)
class RefusalRow:
    id: str
    call: str  # "add" | "search" | "export" (stdio backend) or "hosted-add" (the hosted wire)
    personal_floor: bool
    refuse_headerless: bool  # the agent case: refused even with the floor on
    pre_landed: bool  # False: today's service, where a user_only route never runs the resolver (review M1)
    switch_first: (
        str  # hosted: switch to this workspace before the write ("" = no switch)
    )
    attempts: list[
        tuple[str, str, str | None, str]
    ]  # every data-route decision svc-api made, in order


REFUSAL_ROWS = [
    RefusalRow(
        "stdio-add-floor-off-no-default",
        "add",
        False,
        False,
        True,
        "",
        [("POST", "/memory/add", None, "missing_team_context")],
    ),
    RefusalRow(
        "stdio-add-agent-floor-on",
        "add",
        True,
        True,
        True,
        "",
        [("POST", "/memory/add", None, "missing_team_context")],
    ),
    RefusalRow(
        "stdio-search-floor-off-no-default",
        "search",
        False,
        False,
        True,
        "",
        [("POST", "/memory/search", None, "missing_team_context")],
    ),
    RefusalRow(
        "stdio-export-floor-off-no-default",
        "export",
        False,
        False,
        True,
        "",
        [("GET", "/memory/okf/export", None, "missing_team_context")],
    ),
    RefusalRow(
        "hosted-add-floor-off-no-default",
        "hosted-add",
        False,
        False,
        True,
        "",
        [("POST", "/memory/add", None, "missing_team_context")],
    ),
    # Today's service (M1): /memory/add is user_only, so even a saved session pick does not route a header-less add.
    # The MCP must not paper over it with the default or the session's workspace as a header.
    RefusalRow(
        "hosted-add-after-switch-today-user-only",
        "hosted-add",
        True,
        False,
        False,
        "team-B",
        [("POST", "/memory/add", None, "missing_team_context")],
    ),
]


@pytest.mark.parametrize("row", REFUSAL_ROWS, ids=[r.id for r in REFUSAL_ROWS])
def test_a_refusal_is_a_clear_error_and_is_never_retried(
    monkeypatch, tmp_path, row: RefusalRow
) -> None:
    if row.call == "hosted-add":
        harness = _Harness()
        harness.routing.personal_floor = row.personal_floor
        harness.routing.refuse_headerless = row.refuse_headerless
        harness.routing.pre_landed = row.pre_landed
        harness.routing.default_team_id = (
            None  # no account default: the service refuses
        )
        harness.routing.install(monkeypatch, fake_svc_api())
        wire = _wire(monkeypatch)

        async def body(client):
            session_id = await harness.session(client)
            if row.switch_first:
                await harness.call(
                    client,
                    session_id,
                    "switch_workspace",
                    {"workspace": row.switch_first},
                )
            wire.clear()
            harness.routing.resolved.clear()
            first = await harness.call(
                client, session_id, "memory_add", {"content": "x"}
            )
            second = await harness.call(
                client, session_id, "memory_add", {"content": "y"}
            )
            return first, second

        first, second = harness.run(body)
        assert first.get("isError") is True and second.get("isError") is True
        assert MISSING_WORKSPACE_HELP in _text(first), _text(first)
        assert harness.routing.resolved == row.attempts * 2
    else:
        store = RoutingSessionStore(personal_floor=row.personal_floor)
        store.refuse_headerless = row.refuse_headerless
        store.pre_landed = row.pre_landed
        backend, _ = _stdio(monkeypatch, store)
        wire = _wire(monkeypatch)
        archive = tmp_path / "out.okf.tar.gz"
        call = {
            "add": lambda: backend.add("x"),
            "search": lambda: backend.search("x"),
            "export": lambda: backend.export_okf(str(archive)),
        }[row.call]
        for _ in range(
            2
        ):  # the second call proves nothing was armed by the first refusal
            with pytest.raises(RuntimeError) as raised:
                call()
            assert str(raised.value) == MISSING_WORKSPACE_HELP
        assert not archive.exists()
        assert store.resolved == row.attempts * 2

    # One attempt per call, never a header: no default, explain answer or session workspace was turned into a pick.
    assert [h.get("X-Workspace-Id") for h in wire] == [None, None]


# --- 5b. the review's probes (WS-ROUTING-5 review H1, H2): no fabricated header on any path -----------------------------


@dataclass(frozen=True)
class ProbeRow:
    id: str
    explain: str  # how GET /memory/routing/explain answers: "timeout" | "503" | "malformed" | "unavailable" | "real"
    hosted: bool
    save_session: str  # a session pick saved server-side (PUT /memory/routing/session) after the first write
    routed: list[
        tuple[str | None, str]
    ]  # (workspace, routed_by) of each write, in order


PROBE_ROWS = [
    # H1, probe 1: a refusal followed by an explain the MCP cannot read is one attempt, never a retry with the default.
    ProbeRow(
        "explain-timeout", "timeout", False, "", [(None, "missing_team_context")] * 2
    ),
    ProbeRow("explain-503", "503", False, "", [(None, "missing_team_context")] * 2),
    ProbeRow(
        "explain-malformed",
        "malformed",
        False,
        "",
        [(None, "missing_team_context")] * 2,
    ),
    # H2, probe 2: a pick saved server-side after a refusal wins the next write (nothing cached a default).
    ProbeRow(
        "stdio-server-side-pick-after-refusal",
        "real",
        False,
        "team-B",
        [(None, "missing_team_context"), ("team-B", "session")],
    ),
    # H1, probe 3: hosted, switched to team-B; explain reports the session store unavailable while the data resolver
    # still reads the pick. Writes stay header-less and land in team-B.
    ProbeRow(
        "hosted-unavailable-session-explain",
        "unavailable",
        True,
        "",
        [("team-B", "session")] * 2,
    ),
]

_UNAVAILABLE_EXPLAIN = {
    "workspace_id": None,
    "routed_by": None,
    "candidates": [
        {"lever": "call", "workspace_id": None, "status": "absent"},
        {"lever": "session", "workspace_id": None, "status": "unavailable"},
    ],
    "notices": [{"code": "session_expired", "detail": "session store unavailable"}],
    "flags": {"personal_floor": False},
}


def _explain_answer(kind: str, request: httpx.Request) -> httpx.Response:
    if kind == "timeout":
        raise httpx.ReadTimeout("explain timed out", request=request)
    if kind == "503":
        return httpx.Response(503, json={"detail": "down"}, request=request)
    if kind == "malformed":
        return httpx.Response(200, content=b"{not json", request=request)
    return httpx.Response(200, json=_UNAVAILABLE_EXPLAIN, request=request)


@pytest.mark.parametrize("row", PROBE_ROWS, ids=[r.id for r in PROBE_ROWS])
def test_the_review_probes_never_fabricate_a_header(monkeypatch, row: ProbeRow) -> None:
    if row.hosted:
        harness = _Harness()
        store = harness.routing
        store.personal_floor = False
        store.install(monkeypatch, fake_svc_api())
        wire = _wire(monkeypatch)
        real_handle = store.handle

        def handle(request: httpx.Request):
            if request.url.path == "/memory/routing/explain" and store.rows:
                return _explain_answer(row.explain, request)
            return real_handle(request)

        monkeypatch.setattr(store, "handle", handle)

        async def body(client):
            session_id = await harness.session(client)
            await harness.call(
                client, session_id, "switch_workspace", {"workspace": "team-B"}
            )
            wire.clear()
            store.resolved.clear()
            results = [
                await harness.call(client, session_id, "memory_add", {"content": n})
                for n in ("x", "y")
            ]
            return results

        results = harness.run(body)
        assert [r.get("isError") is True for r in results] == [False, False]
    else:
        store = RoutingSessionStore(personal_floor=False)
        backend, _ = _stdio(
            monkeypatch, store
        )  # the /auth/me default is DEFAULT: display only
        wire = _wire(monkeypatch)
        if row.explain != "real":

            def explain_get(url: str, **kwargs: Any) -> httpx.Response:
                return _explain_answer(row.explain, httpx.Request("GET", url))

            monkeypatch.setattr(
                "smartmemory_mcp.backends.remote.httpx.get", explain_get
            )
        with pytest.raises(RuntimeError, match="switch_workspace"):
            backend.add("x")
        if row.save_session:
            store.handle(
                httpx.Request(
                    "PUT",
                    f"{API}/memory/routing/session",
                    headers={"Authorization": "Bearer sk_test"},
                    json={
                        "workspace_id": row.save_session,
                        "client_key": backend.client_key,
                    },
                )
            )
            assert backend.add("y") == "mem-1"
        else:
            with pytest.raises(RuntimeError, match="switch_workspace"):
                backend.add("y")

    assert [h.get("X-Workspace-Id") for h in wire] == [None, None]
    assert [r[2:] for r in store.resolved] == row.routed


# --- 6. the hosted container's env never becomes a caller's pick --------------------------------------------------------

# switch first? -> (workspace, routed_by) of the write. The container env names a workspace no caller picked.
CONTAINER_ENV_ROWS = [
    pytest.param(False, ("ws_personal_user-1", "account_default"), id="no-switch"),
    pytest.param(True, ("team-B", "session"), id="after-switch"),
]


@pytest.mark.parametrize(("switch", "routed"), CONTAINER_ENV_ROWS)
def test_the_hosted_container_env_never_becomes_a_pick(
    monkeypatch, switch: bool, routed: tuple[str, str]
) -> None:
    monkeypatch.setenv("SMARTMEMORY_WORKSPACE_ID", "ws-container")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "team-container")
    harness = _Harness()
    harness.routing.personal_floor = True
    spy = _Spy(fake_svc_api())
    harness.routing.install(monkeypatch, spy)

    async def body(client):
        session_id = await harness.session(client)
        if switch:
            await harness.call(
                client, session_id, "switch_workspace", {"workspace": "team-B"}
            )
        spy.sent.clear()
        harness.routing.resolved.clear()
        return await harness.call(client, session_id, "memory_add", {"content": "x"})

    result = harness.run(body)

    assert result.get("isError") is not True, _text(result)
    assert [h.get("X-Workspace-Id") for h in spy.sent] == [None]
    assert harness.routing.resolved == [("POST", "/memory/add", *routed)]


# --- fold-in WS-ROUTING-4-FU-MCP-SESSION-REFRESH-1: a switch survives continuous use past the 24 h window -----------------

HOUR = 3600.0


def test_a_hosted_switch_survives_more_than_24_hours_of_continuous_use(
    monkeypatch,
) -> None:
    """Switch, then one write an hour for 26 h on the advancing clock: every write still lands in the pick, routed by
    the session. Before WS-ROUTING-5 the pick went out as X-Workspace-Id (routed `call`), so no write refreshed the
    session row and it lapsed at 24 h; later writes went to the default workspace."""
    harness = _Harness()
    harness.routing.install(monkeypatch, fake_svc_api())

    async def body(client):
        session_id = await harness.session(client)
        await harness.call(
            client, session_id, "switch_workspace", {"workspace": "team-B"}
        )
        landed = []
        for hour in range(1, 27):
            harness.routing.now = hour * HOUR
            harness.routing.resolved.clear()
            await harness.call(
                client, session_id, "memory_add", {"content": f"note {hour}"}
            )
            landed.append((hour, *harness.routing.resolved[-1][2:]))
        return landed

    landed = harness.run(body)

    assert landed[-1] == (26, "team-B", "session")
    assert [row for row in landed if row[1:] != ("team-B", "session")] == []


# --- the stdio write-target line (`routing_middleware.RemoteRoutingMiddleware`) -------------------------------------------

# (pick, tool, arguments) -> the line the result ends with (None: a read-only tool gets no line).
STDIO_TARGET_ROWS = [
    pytest.param(
        "ws-b",
        "memory_add",
        {"content": "x"},
        "Workspace: ws-b, routed by call.",
        id="pick",
    ),
    pytest.param(
        "",
        "memory_add",
        {"content": "x"},
        "Workspace: ws_personal_user-1, routed by account_default.",
        id="no-pick-floor",
    ),
    pytest.param("ws-b", "memory_get", {"item_id": "mem-1"}, None, id="read-only"),
]


@pytest.mark.parametrize(("pick", "tool", "arguments", "line"), STDIO_TARGET_ROWS)
def test_a_stdio_write_names_its_target_workspace(
    monkeypatch, pick: str, tool: str, arguments: dict[str, Any], line: str | None
) -> None:
    import anyio
    from fastmcp import Client

    from smartmemory_mcp.server import mcp

    store = RoutingSessionStore(personal_floor=True)
    _, spy = _stdio(monkeypatch, store, team_id=pick)

    async def call():
        async with Client(mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    result = anyio.run(call)
    texts = [part.text for part in result.content]

    assert not result.is_error, texts
    assert (texts[-1] if line else None) == line
    assert line is not None or not any(t.startswith("Workspace:") for t in texts)
    assert all(h["X-SM-Client"].startswith("mcp:") for h in spy.sent)
