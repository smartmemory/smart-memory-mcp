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
    out = _tools()["review_queue_settle"]("rev 1", "keep_b", "checked")
    assert "api_key:u1" in out
    assert backend.calls == [
        (
            "POST",
            "/memory/reasoning/reviews/rev%201/settle",
            {"json": {"action": "keep_b", "reason": "checked"}},
        )
    ]


@pytest.mark.parametrize("bad_id", ["..", "../x", "a/b"])
def test_settle_refuses_an_id_that_could_reach_another_route(monkeypatch, bad_id):
    backend = _use(monkeypatch, _Remote({}))
    with pytest.raises(ValueError, match="Invalid id"):
        _tools()["review_queue_settle"](bad_id, "keep_b", "checked")
    assert backend.calls == []


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


class _LiteSettings:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "memory.db"
        self.doc = {"review.supersession": None, "review.approval": None}
        self.sets: list[dict] = []

    def read(self):
        return dict(self.doc)

    def set(self, *, updated_by, **changes):
        self.sets.append({"updated_by": updated_by, **changes})
        for knob, value in changes.items():
            self.doc[f"review.{knob}"] = value
        return self.read()


def _lite(monkeypatch, tmp_path):
    backend = _use(monkeypatch, _Local())
    settings = _LiteSettings(tmp_path)
    backend._mem.scope_provider = type("P", (), {"lite_settings": settings})()
    return settings


def test_policy_tools_read_and_tighten_the_lite_store_settings(monkeypatch, tmp_path):
    # Part 7 (2.0.2): was "remote only"; the local tools now read and set the store's own settings row.
    settings = _lite(monkeypatch, tmp_path)
    tools = _tools()
    assert json.loads(tools["supersession_policy_get"]())["policy"] == "retire"
    approval = json.loads(tools["review_approval_get"]())
    assert approval["policy"] == "person_only" and approval["source"] == "default"
    assert (
        json.loads(tools["supersession_policy_set"]("propose"))["stated"] == "propose"
    )
    assert (
        json.loads(tools["review_approval_set"]("person_only"))["stated"]
        == "person_only"
    )
    assert settings.sets == [
        {"updated_by": "local:mcp", "supersession": "propose"},
        {"updated_by": "local:mcp", "approval": "person_only"},
    ]


@pytest.mark.parametrize(
    ("tool", "value", "command"),
    [
        ("review_approval_set", "anyone", "review-settings --approval anyone"),
        ("review_approval_set", "inherit", "review-settings --approval unset"),
        ("supersession_policy_set", "retire", "review-settings --hold off"),
        ("supersession_policy_set", "inherit", "review-settings --hold unset"),
    ],
)
def test_the_local_tool_never_loosens_and_names_the_person_command(
    monkeypatch, tmp_path, tool, value, command
):
    settings = _lite(monkeypatch, tmp_path)
    out = _tools()[tool](value)
    assert out.startswith("Refused: the local connector is not a person"), out
    assert f"smartmemory --data-dir {tmp_path} {command}" in out
    assert settings.sets == []


def test_policy_tools_without_a_lite_store_say_so(monkeypatch):
    _use(monkeypatch, _Local())
    tools = _tools()
    for out in (
        tools["supersession_policy_get"](),
        tools["review_approval_get"](),
        tools["review_approval_set"]("person_only"),
    ):
        assert "no settings row" in out


def test_remote_approval_pair_uses_the_settings_route(monkeypatch):
    settings = {
        **SETTINGS,
        "effective": {"review": {"approval": "person_only"}},
        "source": {"review": {"approval": "default"}},
        "stated": {"review": {}},
    }
    backend = _use(monkeypatch, _Remote(settings))
    tools = _tools()
    view = json.loads(tools["review_approval_get"]())
    assert view["policy"] == "person_only" and view["source"] == "default"
    tools["review_approval_set"]("inherit")
    assert backend.calls[-1] == (
        "PATCH",
        "/memory/workspaces/ws%201/settings",
        {"json": {"stated": {"review": {"approval": None}}}},
    )
    assert tools["review_approval_set"]("nobody").startswith("approval must be")


def test_a_403_sentence_is_shown_as_the_service_wrote_it(monkeypatch):
    sentence = "Only a person can approve reviews in this workspace (review approval is person_only); x"
    _use(
        monkeypatch,
        _Remote(
            {
                "error": "API error 403: {...}",
                "status_code": 403,
                "body": {"detail": sentence},
            }
        ),
    )
    assert (
        _tools()["review_queue_settle"]("conflict_a", "keep_b", "r")
        == f"Error (403): {sentence}"
    )
    _use(
        monkeypatch,
        _Remote(
            {
                "error": "x",
                "status_code": 403,
                "body": {
                    "detail": {
                        "code": "c",
                        "message": "Only a signed-in person may loosen review.approval",
                    }
                },
            }
        ),
    )
    assert (
        _tools()["review_approval_set"]("anyone")
        == "Error (403): Only a signed-in person may loosen review.approval"
    )


def test_list_shows_the_total(monkeypatch):
    _use(
        monkeypatch,
        _Remote(
            {"items": [ROW], "total": 55, "status": "open", "limit": 1, "offset": 0}
        ),
    )
    assert _tools()["review_queue_list"](limit=1).startswith(
        "1 of 55 open conflict review(s)"
    )
    backend = _use(monkeypatch, _Local())
    backend._mem.count_conflict_reviews = lambda *, status: 3
    assert _tools()["review_queue_list"]().startswith("1 of 3 open conflict review(s)")
