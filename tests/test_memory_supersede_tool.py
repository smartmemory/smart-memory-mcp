"""memory_supersede (DHRUV-CODING-RULE-P0-1, bug 1).

The only "change an existing memory" tool used to be memory_update, an in-place
PATCH that keeps no history. A reversed rule needs the service's supersede
operation: a new version, the old one retained and closed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS, MEMORY_TOOLS
from smartmemory_mcp.tools import memory_tools


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, *_args, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def wire(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "superseded",
                "old_item_id": "old-1",
                "new_item_id": "new-1",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request",
        lambda method, url, **kw: client.request(
            method, url, **{k: v for k, v in kw.items() if k != "timeout"}
        ),
    )
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
    return seen


@pytest.fixture
def tools():
    mcp = _FakeMCP()
    memory_tools.register_free(mcp)
    memory_tools.register_pro(mcp)
    return mcp.fns


def test_memory_supersede_posts_to_the_supersede_route(wire, tools):
    out = tools["memory_supersede"](
        "old-1",
        "Code influenza to lab-confirmed.",
        "coding lead reversed the rule",
        metadata={"site": "all"},
        valid_from="2026-04-20",
    )

    (request,) = wire
    body = json.loads(request.content)
    assert (request.method, request.url.path) == ("POST", "/memory/old-1/supersede")
    assert body["content"] == "Code influenza to lab-confirmed."
    assert body["reason"] == "coding lead reversed the rule"
    assert body["metadata"] == {"site": "all"}
    assert body["valid_from"] == "2026-04-20"
    assert body["memory_type"]
    assert "new-1" in out and "old-1" in out


def test_memory_supersede_omits_valid_from_when_absent(wire, tools):
    tools["memory_supersede"]("old-1", "new rule", "reversal")

    assert "valid_from" not in json.loads(wire[0].content)


def test_memory_supersede_is_on_the_hosted_allowlist():
    assert "memory_supersede" in MEMORY_TOOLS
    assert "memory_supersede" in HOSTED_TOOLS


def test_memory_update_docstring_says_in_place_and_points_at_supersede(tools):
    doc = " ".join((tools["memory_update"].__doc__ or "").split()).lower()

    assert "in place" in doc
    assert "no history" in doc
    assert "memory_supersede" in doc


def test_memory_ingest_docstring_points_a_changing_reply_at_supersede(tools):
    doc = tools["memory_ingest"].__doc__ or ""

    assert "memory_supersede" in doc


class _RecordingMem:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.superseding: list = []
        self.updates: list = []

    def get(self, item_id):
        return {"item_id": item_id}

    def ingest_superseding(self, item, *, supersedes, **kwargs):
        self.superseding.append((item, supersedes, kwargs))
        return None if self.fail else "new-1"

    def update_properties(self, item_id, properties, *a, **k):
        self.updates.append((item_id, properties))


def _local(mem):
    from smartmemory_mcp.backends.local import LocalBackend

    backend = object.__new__(LocalBackend)
    backend._mem = mem
    return backend


def test_local_supersede_closes_the_interval_inside_core_supersession():
    """Codex pre-land F4: valid_from goes to core's compensated unit; no second write here."""
    from datetime import datetime, timezone

    mem = _RecordingMem()
    _local(mem).supersede(
        "old-1", "Code to lab-confirmed.", "reversal", valid_from="2026-04-20"
    )

    item, supersedes, kwargs = mem.superseding[0]
    expected = datetime(2026, 4, 20, tzinfo=timezone.utc)
    assert supersedes == "old-1" and kwargs == {"valid_from": expected}
    assert item.valid_start_time == expected
    assert mem.updates == []


def test_local_supersede_failure_writes_nothing_more():
    mem = _RecordingMem(fail=True)
    with pytest.raises(RuntimeError):
        _local(mem).supersede(
            "old-1", "Code to lab-confirmed.", "reversal", valid_from="2026-04-20"
        )
    assert mem.updates == []


def test_local_supersede_without_valid_from_passes_no_interval():
    mem = _RecordingMem()
    _local(mem).supersede("old-1", "Code to lab-confirmed.", "reversal")
    assert mem.superseding[0][2] == {} and mem.updates == []
