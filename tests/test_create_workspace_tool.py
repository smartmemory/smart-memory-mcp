"""WS-POLICY-10 B: `create_workspace(name)` on stdio and hosted MCP.

It posts `/memory/workspaces` with no `team_id` (the caller's private, Team-less workspace), sends no `X-Workspace-Id`,
returns the new id, and never moves the session: stdio keeps its workspace, hosted writes no routing record (Q-D,
default by sm20-main, not owner-set). A refusal is reported and changes nothing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend

from ._hosted_fixtures import API_KEY
from ._tools import tool_fn
from .test_hosted_identity import _text
from .test_hosted_tools_allowlist import _Harness, fake_svc_api


@pytest.fixture(autouse=True)
def isolate_tier(monkeypatch):
    # Do not consult the user's stored credentials during server registration.
    monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "")


def _stdio_backend(monkeypatch, status: int, body: dict[str, Any]):
    seen: list[tuple[str, str, dict[str, str], Any]] = []

    def request(method, url, **kwargs):
        seen.append((method, url, kwargs["headers"], kwargs.get("json")))
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    backend = RemoteBackend(
        api_url="http://stdio-create.test", api_key="test-key", team_id="ws-a"
    )
    backend._session["_bootstrapped"] = True
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", request)
    monkeypatch.setattr("smartmemory_mcp.server.get_backend", lambda: backend)
    return backend, seen


def test_stdio_create_posts_no_team_and_keeps_the_session(monkeypatch):
    backend, seen = _stdio_backend(
        monkeypatch, 201, {"workspace_id": "ws-new", "name": "Scratch"}
    )

    text = tool_fn("create_workspace")(name="  Scratch ")

    assert seen == [
        (
            "POST",
            "http://stdio-create.test/memory/workspaces",
            {"Authorization": "Bearer test-key", "Content-Type": "application/json"},
            {"name": "Scratch"},
        )
    ]
    assert "Created private workspace: Scratch (ws-new)" in text
    assert "switch_workspace('ws-new')" in text
    assert backend.active_workspace_id == "ws-a"


def test_stdio_create_sends_the_description(monkeypatch):
    _, seen = _stdio_backend(
        monkeypatch, 201, {"workspace_id": "ws-new", "name": "Scratch"}
    )
    tool_fn("create_workspace")(name="Scratch", description="tmp")
    assert seen[0][3] == {"name": "Scratch", "description": "tmp"}


# (id, status, body) -> the refusal is reported and the session does not move.
REFUSAL_ROWS = [
    ("agent-key-403", 403, {"detail": {"code": "agent_workspace_admin"}}),
    (
        "bound-key-403",
        403,
        {
            "detail": "A workspace-bound API key cannot create teams, workspaces or agents"
        },
    ),
    ("audit-503", 503, {"detail": {"code": "workspace_audit_unavailable"}}),
]


@pytest.mark.parametrize(
    ("status", "body"), [pytest.param(s, b, id=i) for i, s, b in REFUSAL_ROWS]
)
def test_stdio_create_refusal_is_reported(monkeypatch, status, body):
    backend, _ = _stdio_backend(monkeypatch, status, body)
    text = tool_fn("create_workspace")(name="Scratch")
    assert text.startswith("Could not create the workspace:")
    assert f"API error {status}" in text
    assert backend.active_workspace_id == "ws-a"


def test_stdio_create_refuses_a_blank_name_without_a_request(monkeypatch):
    _, seen = _stdio_backend(monkeypatch, 201, {"workspace_id": "ws-new"})
    assert (
        tool_fn("create_workspace")(name="  ")
        == "Could not create the workspace: name is required."
    )
    assert seen == []


def test_stdio_create_refuses_lite(monkeypatch):
    backend = object.__new__(LocalBackend)
    monkeypatch.setattr("smartmemory_mcp.server.get_backend", lambda: backend)
    assert "Lite (local mode) has no workspaces" in tool_fn("create_workspace")(
        name="Scratch"
    )


def test_hosted_create_returns_the_id_and_writes_no_session_record(monkeypatch):
    harness = _Harness()
    workspace_headers: list[dict[str, str]] = []
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request",
        fake_svc_api(harness.seen, team_headers=workspace_headers),
    )

    async def body(client):
        session_id = await harness.session(client)
        created = await harness.call(
            client, session_id, "create_workspace", {"name": "Scratch"}
        )
        who = await harness.call(client, session_id, "whoami")
        return created, who

    created, who = harness.run(body)

    assert created.get("isError") is not True, _text(created)
    assert "Created private workspace: Scratch (ws-new)" in _text(created)
    assert ("POST", "/memory/workspaces") in harness.seen
    assert all("x-workspace-id" not in sent for sent in workspace_headers)
    assert [call for call in harness.routing.calls if call[0] == "PUT"] == []
    assert (API_KEY, "mcp:test") not in harness.routing.rows
    assert "ws-new" not in _text(who)
