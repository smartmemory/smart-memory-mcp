"""BUG-DECISION-HISTORY-PAGE-TIE-1 note H2 (MCP tool): the limit-2 cursor walk over a real service.

The service, its stores and the 6-entry history (three entries at one instant) are prepared by the unit's
service-side driver, which writes a seed file; ``SM_DV_HISTORY_SEED`` names it. Without it the test skips.
"""

from __future__ import annotations

import json
import os

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import decision_tools
from smartmemory_mcp.tools.common import history_text


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def seed():
    path = os.environ.get("SM_DV_HISTORY_SEED")
    if not path or not os.path.exists(path):
        pytest.skip("SM_DV_HISTORY_SEED not set: no seeded service to walk")
    with open(path) as handle:
        return json.load(handle)


def test_h2_mcp_tool_cursor_walk_returns_all_six_once(seed, monkeypatch):
    backend = RemoteBackend(api_url=seed["base_url"], api_key=seed["token"], team_id=seed["workspace_id"])
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    tool = mcp.tools["decision_history"]
    did = seed["decision_id"]
    whole = backend.decision_history(did, limit=500)
    assert whole["entries"] == seed["entries"] and len(whole["entries"]) == 6
    walked, cursor = [], None
    for _ in range(10):
        page = backend.decision_history(did, limit=2, cursor=cursor)
        walked += page["entries"]
        if not page["truncated"]:
            break
        cursor = page["next_cursor"]
    assert walked == whole["entries"]
    # The tool itself: the first page tells the reader to pass the cursor, and passing it gives the next page.
    first = tool(decision_id=did, limit=2)
    assert "ask again with cursor set to" in first
    token = first.rsplit("cursor set to ", 1)[1].rstrip(".")
    second = tool(decision_id=did, limit=2, cursor=token)
    assert second != first and history_text(did, backend.decision_history(did, limit=2, cursor=token)) == second
    with pytest.raises(Exception, match="invalid_cursor"):
        tool(decision_id=did, cursor="garbage")
