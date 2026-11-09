"""WS-ROUTING-5 on 2.0, shape B (s1-chat 2026-11-09 "keep shape B, leftovers only").

The header rule itself does not change on 2.0: `X-Workspace-Id` is a per-call argument, else the saved pick
(`switch_workspace`), else the session team (env/config or the account default). What this unit adds and these rows
pin: `X-SM-Client` on every remote and hosted call, and whoami's `Routed by:` line naming where the workspace came from.
No MCP tool takes a workspace argument on 2.0; the per-call argument is the backend's `workspace_id=` (the procedure
pin, `memory_get` on a parked workspace), exercised here directly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.backends.remote import (
    CLIENT_KEY_FALLBACK,
    ROUTED_BY_CONFIG,
    ROUTED_BY_DEFAULT,
    ROUTED_BY_PICK,
    RemoteBackend,
)
from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.tier import Tier
from smartmemory_mcp.tools import common

from ._tools import tool_fn
from .test_hosted_tools_allowlist import ITEM, _Harness, _text, fake_svc_api

API = "http://svc.test"
WORKSPACES = [
    {"workspace_id": "ws-a", "name": "Alpha", "tenant_id": "tenant-1"},
    {"workspace_id": "ws-b", "name": "Beta", "tenant_id": "tenant-1"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    before = hosted_identity.hosted_mode_enabled()
    hosted_identity.set_hosted_mode(False)
    monkeypatch.setattr("smartmemory_mcp.server.resolve_tier", lambda: Tier.FREE)
    for name in ("SMARTMEMORY_WORKSPACE_ID", "SMARTMEMORY_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        dispatch.reset_backend()
        common.reset_backend()
        hosted_identity.set_hosted_mode(before)


class Service:
    """svc-api for the stdio remote backend; keeps every non-listing request's headers."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path
        request = httpx.Request(method, url)
        if path == "/memory/workspaces":
            return httpx.Response(200, json={"workspaces": WORKSPACES}, request=request)
        self.sent.append((path, dict(kwargs.get("headers") or {})))
        if path == "/memory/add":
            return httpx.Response(200, json={"item_id": "mem-1"}, request=request)
        return httpx.Response(200, json=ITEM, request=request)

    def last(self) -> dict[str, str]:
        return self.sent[-1][1]


def _auth_me(url: str, **_kwargs: Any) -> httpx.Response:
    body = {"email": "dev@test.com", "tenant_id": "tenant-1", "default_team_id": "ws-a"}
    return httpx.Response(200, json=body, request=httpx.Request("GET", url))


def _process_backend(monkeypatch, team_id: str = "") -> tuple[RemoteBackend, Service]:
    service = Service()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", service)
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.get", _auth_me)
    backend = RemoteBackend(api_url=API, api_key="sk_test", team_id=team_id)
    monkeypatch.setattr(dispatch, "_backend", backend)
    monkeypatch.setattr(common, "_backend", backend)
    return backend, service


def test_no_pick_no_argument_sends_the_session_team(monkeypatch) -> None:
    _, service = _process_backend(monkeypatch)

    tool_fn("memory_save")(content="a note")
    who = tool_fn("whoami")()

    assert (
        service.last()["X-Workspace-Id"] == "ws-a"
    )  # the discovered default, unchanged
    assert service.last()["X-SM-Client"] == CLIENT_KEY_FALLBACK
    assert f"Routed by: {ROUTED_BY_DEFAULT}" in who
    assert "switch_workspace(workspace)" in who


def test_a_call_argument_overrides_the_session_team(monkeypatch) -> None:
    backend, service = _process_backend(monkeypatch)

    backend.get("mem-1", workspace_id="ws-arg")

    assert service.last()["X-Workspace-Id"] == "ws-arg"
    assert service.last()["X-SM-Client"] == CLIENT_KEY_FALLBACK
    assert (
        backend.active_workspace_id == "ws-a"
    )  # one call only, the session is untouched
    assert backend.routed_by == ROUTED_BY_DEFAULT


def test_after_switch_workspace_the_next_write_carries_the_pick(monkeypatch) -> None:
    _, service = _process_backend(monkeypatch)

    switched = tool_fn("switch_workspace")(workspace="Beta")
    saved = tool_fn("memory_save")(content="a note")
    who = tool_fn("whoami")()

    assert switched.startswith("Switched to workspace")
    assert service.last()["X-Workspace-Id"] == "ws-b"
    assert service.last()["X-SM-Client"] == CLIENT_KEY_FALLBACK
    assert f"Routed by: {ROUTED_BY_PICK}" in who
    assert "Workspace: Beta (ws-b)" in who
    assert saved.structured_content["workspace"] == "Beta (ws-b)"


def test_a_configured_team_says_config_env(monkeypatch) -> None:
    _, service = _process_backend(monkeypatch, team_id="ws-b")

    tool_fn("memory_save")(content="a note")
    who = tool_fn("whoami")()

    assert service.last()["X-Workspace-Id"] == "ws-b"
    assert f"Routed by: {ROUTED_BY_CONFIG}" in who
    assert "SMARTMEMORY_WORKSPACE_ID" in who


def test_hosted_sends_the_client_name_and_whoami_names_the_source(monkeypatch) -> None:
    sent: list[dict[str, str]] = []

    def spy(method, url, **kwargs):
        sent.append(dict(kwargs.get("headers") or {}))
        return fake_svc_api()(method, url, **kwargs)

    harness = _Harness()
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", spy)

    async def body(client):
        session_id = await harness.session(client)
        before = _text(await harness.call(client, session_id, "whoami"))
        sent.clear()
        await harness.call(client, session_id, "memory_search", {"query": "q"})
        default_headers = list(sent)
        await harness.call(
            client, session_id, "switch_workspace", {"workspace": "team-B"}
        )
        sent.clear()
        await harness.call(client, session_id, "memory_save", {"content": "a note"})
        after = _text(await harness.call(client, session_id, "whoami"))
        return before, default_headers, list(sent), after

    before, default_headers, pick_headers, after = harness.run(body)

    assert f"Routed by: {ROUTED_BY_DEFAULT}" in before
    assert f"Routed by: {ROUTED_BY_PICK}" in after
    assert default_headers and pick_headers
    assert all(h["X-SM-Client"] == "mcp:test" for h in default_headers + pick_headers)
    assert all(h["X-Workspace-Id"] == "team-personal" for h in default_headers)
    assert all(h["X-Workspace-Id"] == "team-B" for h in pick_headers)
