"""DECISION-RESTORE-ONE-ACTION-1: the ``decision_restore`` MCP tool on a real Lite store behind ``LocalBackend``.

One call makes a wrongly retired decision active again and leaves its replacement active; a refusal renders the
contract code as text and writes nothing. The route's owner, workspace and race rows run in smart-memory-common and
smart-memory-service.
"""

from __future__ import annotations

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import decision_tools


@pytest.fixture
def local(tmp_path):
    pytest.importorskip("smartmemory.decisions.restore")
    from smartmemory.tools.factory import create_lite_memory

    mem = create_lite_memory(str(tmp_path / "restore"), spawn_worker=False)
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = mem
    try:
        yield backend
    finally:
        close = getattr(mem, "close", None)
        if close:
            close()


def _tool(backend, monkeypatch):
    tools = {}

    class _MCP:
        def tool(self, **_kw):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    decision_tools.register(_MCP())
    return tools["decision_restore"]


def _retired(mem):
    from smartmemory.models.decision import Decision

    old = mem.add_decision("Saturday closing time is 6pm")
    new = mem.supersede_decision(
        old.decision_id,
        Decision(content="Friday night shift ends at 2am"),
        reason="auto",
    )
    return old.decision_id, new.decision_id


def test_restore_tool_undoes_the_retirement_and_keeps_the_replacement(
    local, monkeypatch
):
    restore = _tool(local, monkeypatch)
    old_id, new_id = _retired(local._mem)

    out = restore(old_id, "the Friday rule is an exception")

    assert out.startswith(f"Decision restored: {old_id}") and new_id in out
    assert local._mem.get_decision(old_id).status == "active"
    assert local._mem.get_decision(new_id).status == "active"


def test_restore_tool_refusals_render_the_code_and_write_nothing(local, monkeypatch):
    restore = _tool(local, monkeypatch)
    active = local._mem.add_decision("Still in force").decision_id

    assert restore(active, "try").startswith("Restore refused (not_retired)")
    assert restore("dec_doesnotexist", "try") == "Decision not found: dec_doesnotexist"
    assert local._mem.get_decision(active).status == "active"
