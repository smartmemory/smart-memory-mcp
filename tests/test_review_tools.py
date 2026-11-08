"""Review queue and supersession policy MCP tools (OUTREACH-MARIUS-HITL-SURFACE-1).

Same stand-in pattern as ``tests/test_graph_tools_local_backend.py``: the standalone MCP venv ships no
``smartmemory``, so the tools are captured through a fake MCP and run against a backend that exposes ``request``
(remote) or ``_mem`` (local). Asserts the routes, that no actor or workspace is ever sent, the local actor, input
validation before any call, and that error dicts surface their status.
"""

from __future__ import annotations

import json

import pytest

from smartmemory_mcp.backends.interface import BackendCapabilities

ROW = {
    "id": "conflict_a",
    "status": "open",
    "trigger": "supersession_proposal",
    "conflict": {
        "existing_item_id": "item-old",
        "existing_fact": "old",
        "new_fact": "new",
    },
}
SETTINGS = {
    "scope": "workspace",
    "scope_id": "ws1",
    "version": 3,
    "stated": {"review": {"supersession": "propose"}},
    "effective": {"review": {"supersession": "propose"}},
    "source": {"review": {"supersession": "workspace"}},
}


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools():
    from smartmemory_mcp.tools import review_tools

    mcp = _FakeMCP()
    review_tools.register(mcp)
    return mcp.tools


class _Remote(BackendCapabilities):
    active_workspace_id = "ws 1"

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple] = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


class _Mem:
    def __init__(self):
        self.calls: list[tuple] = []

    def list_conflict_reviews(self, *, status, limit, offset):
        self.calls.append(("list", status, limit, offset))
        return [ROW]

    def settle_conflict_review(self, review_id, *, action, reason, actor):
        self.calls.append(("settle", review_id, action, reason, actor))
        if review_id == "missing":
            raise LookupError("Conflict review not found in this workspace")
        if review_id == "stale":
            raise ValueError("Stale proposal snapshots; new review required")
        return {
            **ROW,
            "status": "resolved",
            "resolution_action": action,
            "resolution_reason": reason,
            "resolved_by": actor,
        }


class _Local(BackendCapabilities):
    def __init__(self):
        self._mem = _Mem()


def _use(monkeypatch, backend):
    monkeypatch.setattr(
        "smartmemory_mcp.tools.review_tools.get_backend", lambda: backend
    )
    return backend


def test_all_four_tools_register():
    assert set(_tools()) == {
        "review_queue_list",
        "review_queue_settle",
        "supersession_policy_get",
        "supersession_policy_set",
    }


def test_remote_list_sends_only_status_and_paging(monkeypatch):
    backend = _use(
        monkeypatch,
        _Remote({"items": [ROW], "status": "open", "limit": 50, "offset": 0}),
    )
    out = _tools()["review_queue_list"]()
    assert "conflict_a" in out and "old" in out
    assert backend.calls == [
        (
            "GET",
            "/memory/reasoning/reviews",
            {"params": {"status": "open", "limit": 50, "offset": 0}},
        )
    ]


def test_remote_settle_never_sends_an_actor_and_quotes_the_id(monkeypatch):
    backend = _use(
        monkeypatch, _Remote({**ROW, "status": "resolved", "resolved_by": "api_key:u1"})
    )
    out = _tools()["review_queue_settle"]("../x", "keep_b", "checked")
    assert "api_key:u1" in out
    assert backend.calls == [
        (
            "POST",
            "/memory/reasoning/reviews/..%2Fx/settle",
            {"json": {"action": "keep_b", "reason": "checked"}},
        )
    ]


@pytest.mark.parametrize("status", [404, 403, 409, 500])
def test_remote_errors_surface_their_status(monkeypatch, status):
    _use(monkeypatch, _Remote({"error": "API error", "status_code": status}))
    assert _tools()["review_queue_settle"]("conflict_a", "keep_a", "r").startswith(
        f"Error ({status})"
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("conflict_a", "keep_c", "r"), "action must be"),
        (("conflict_a", "keep_a", "   "), "reason must be"),
        (("conflict_a", "keep_a", "x" * 2001), "reason must be"),
    ],
)
def test_invalid_settle_input_makes_no_call(monkeypatch, args, message):
    backend = _use(monkeypatch, _Remote({}))
    assert _tools()["review_queue_settle"](*args).startswith(message)
    assert backend.calls == []


def test_invalid_list_input_makes_no_call(monkeypatch):
    backend = _use(monkeypatch, _Remote({}))
    tools = _tools()
    assert tools["review_queue_list"](status="closed").startswith("status must be")
    assert tools["review_queue_list"](limit=0).startswith("limit must be")
    assert tools["review_queue_list"](limit=501).startswith("limit must be")
    assert tools["review_queue_list"](offset=-1).startswith("limit must be")
    assert backend.calls == []


def test_local_list_and_settle_use_the_core_facade_with_the_local_actor(monkeypatch):
    backend = _use(monkeypatch, _Local())
    tools = _tools()
    assert "conflict_a" in tools["review_queue_list"](
        status="resolved", limit=5, offset=1
    )
    out = tools["review_queue_settle"]("conflict_a", "keep_both", "both true")
    assert "local:mcp" in out
    assert backend._mem.calls == [
        ("list", "resolved", 5, 1),
        ("settle", "conflict_a", "keep_both", "both true", "local:mcp"),
    ]


def test_local_refusals_are_reported_not_raised(monkeypatch):
    _use(monkeypatch, _Local())
    tools = _tools()
    assert "not found" in tools["review_queue_settle"]("missing", "keep_a", "r")
    assert tools["review_queue_settle"]("stale", "keep_b", "r").startswith(
        "Refused: Stale"
    )


def test_policy_get_reads_the_settings_knob_of_the_session_workspace(monkeypatch):
    backend = _use(monkeypatch, _Remote(SETTINGS))
    view = json.loads(_tools()["supersession_policy_get"]())
    assert view == {
        "workspace_id": "ws1",
        "policy": "propose",
        "source": "workspace",
        "stated": "propose",
        "version": 3,
    }
    assert backend.calls == [("GET", "/memory/workspaces/ws%201/settings", {})]


@pytest.mark.parametrize(
    ("policy", "stated"),
    [("retire", "retire"), ("propose", "propose"), ("inherit", None)],
)
def test_policy_set_patches_the_knob(monkeypatch, policy, stated):
    backend = _use(monkeypatch, _Remote(SETTINGS))
    _tools()["supersession_policy_set"](policy)
    assert backend.calls == [
        (
            "PATCH",
            "/memory/workspaces/ws%201/settings",
            {"json": {"stated": {"review": {"supersession": stated}}}},
        )
    ]


def test_policy_set_rejects_an_unknown_policy_and_refusals_surface(monkeypatch):
    backend = _use(monkeypatch, _Remote({"error": "API error 403", "status_code": 403}))
    tools = _tools()
    assert tools["supersession_policy_set"]("delete").startswith("policy must be")
    assert backend.calls == []
    assert tools["supersession_policy_set"]("retire").startswith("Error (403)")


def test_policy_tools_are_remote_only(monkeypatch):
    _use(monkeypatch, _Local())
    tools = _tools()
    assert "remote backend" in tools["supersession_policy_get"]()
    assert "remote backend" in tools["supersession_policy_set"]("propose")
