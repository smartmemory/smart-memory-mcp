"""Read-only stdio workspace discovery without the WS-ROUTING-5 header rule."""

from types import SimpleNamespace

import httpx
import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend

from ._tools import tool_fn


@pytest.fixture(autouse=True)
def isolate_tier(monkeypatch):
    # Do not consult the user's stored credentials during server registration.
    monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "")


def test_stdio_list_workspaces_formats_two_rows_and_marks_current(monkeypatch):
    rows = [
        {"workspace_id": "ws-a", "name": "Alpha"},
        {"workspace_id": "ws-b", "name": "Beta"},
    ]
    backend = SimpleNamespace(
        _fetch_workspaces=lambda: (rows, ""),
        active_workspace_id="ws-b",
    )
    monkeypatch.setattr("smartmemory_mcp.server.get_backend", lambda: backend)
    assert tool_fn("list_workspaces")() == (
        "  Alpha (ws-a)\n* Beta (ws-b)\n(* = where a write from this client goes now)"
    )


@pytest.mark.parametrize(
    "why", ["API key missing read:teams", "service unavailable (HTTP 503)"]
)
def test_stdio_list_workspaces_none_preserves_main_failure_message(monkeypatch, why):
    backend = SimpleNamespace(_fetch_workspaces=lambda: (None, why))
    monkeypatch.setattr("smartmemory_mcp.server.get_backend", lambda: backend)
    assert tool_fn("list_workspaces")() == f"Could not list your workspaces: {why}"


def test_stdio_list_workspaces_refuses_lite_with_main_message(monkeypatch):
    # The refusal needs no store and must not initialize one.
    backend = object.__new__(LocalBackend)
    monkeypatch.setattr("smartmemory_mcp.server.get_backend", lambda: backend)
    assert tool_fn("list_workspaces")() == (
        "Lite (local mode) has no workspaces: everything is in one local store. "
        "Use remote mode (smartmemory setup --mode remote) for workspaces."
    )


def test_stdio_list_workspaces_sends_no_sm_client_header(monkeypatch):
    seen = []

    def request(method, url, **kwargs):
        seen.append((method, url, kwargs["headers"]))
        return httpx.Response(
            200,
            json={"workspaces": [{"workspace_id": "ws-a", "name": "Alpha"}]},
            request=httpx.Request(method, url),
        )

    backend = RemoteBackend(
        api_url="http://stdio-listws.test", api_key="test-key", team_id="ws-a"
    )
    backend._session["_bootstrapped"] = True
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", request)
    monkeypatch.setattr("smartmemory_mcp.server.get_backend", lambda: backend)
    assert "* Alpha (ws-a)" in tool_fn("list_workspaces")()
    assert seen == [
        (
            "GET",
            "http://stdio-listws.test/memory/workspaces",
            {
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
        )
    ]
    assert all("X-SM-Client" not in headers for _, _, headers in seen)
