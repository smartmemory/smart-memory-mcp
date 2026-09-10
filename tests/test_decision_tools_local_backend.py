"""[bug-hunt 2026-06-02 — CRITICAL] decision writes must reach the real
SmartMemory (``LocalBackend._mem``), never the MCP backend wrapper.

Passing the wrapper made the ManagedType framework call ``LocalBackend.add(memory_item)``
— whose signature is ``add(content: str, ...)`` — so the MemoryItem was received as
``content`` and silently re-wrapped: decision_type/confidence/rationale/item_id all
discarded, stored as a semantic node, unreadable afterward. The tool reported success
while writing garbage.

MCP-REMOTE-DECISIONS-1 moved the manager construction out of the tool and into
``LocalBackend.decision_*`` so the tool can be transport-agnostic. The ``_mem``
requirement is unchanged and is still pinned here — at its new home.
"""

from unittest.mock import MagicMock, patch

from smartmemory_mcp.backends.local import LocalBackend


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools():
    from smartmemory_mcp.tools import decision_tools

    mcp = _FakeMCP()
    decision_tools.register(mcp)
    return mcp.tools


def _local_backend(mem=None):
    """A LocalBackend without running its smartmemory-importing __init__."""
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = mem if mem is not None else object()
    return backend


def test_decision_create_builds_manager_with_local_sm_not_wrapper():
    """Forcing function: DecisionManager is constructed with the backend's ``_mem``
    (the real SmartMemory), never the MCP wrapper that would corrupt the write."""
    tools = _tools()
    backend = _local_backend()
    captured = {}

    class _FakeManager:
        def __init__(self, mem):
            captured["mem"] = mem

        def create(self, **kw):
            return {
                "decision_id": "dec_abc123",
                "decision_type": kw.get("decision_type"),
                "confidence": kw.get("confidence"),
            }

    with (
        patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb,
        patch("smartmemory.decisions.manager.DecisionManager", _FakeManager),
    ):
        gb.return_value = backend
        out = tools["decision_create"](
            content="Use JWT", decision_type="preference", confidence=0.9
        )

    assert captured.get("mem") is backend._mem, (
        "DecisionManager must be built with backend._mem (SmartMemory), not the MCP wrapper"
    )
    assert "dec_abc123" in out


def test_decision_list_builds_queries_with_local_sm_not_wrapper():
    tools = _tools()
    backend = _local_backend()
    captured = {}

    class _FakeQueries:
        def __init__(self, mem):
            captured["mem"] = mem

        def get_active_decisions(self, **kw):
            return []

    with (
        patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb,
        patch("smartmemory.decisions.queries.DecisionQueries", _FakeQueries),
    ):
        gb.return_value = backend
        out = tools["decision_list"]()

    assert captured.get("mem") is backend._mem
    assert out == "No active decisions found."


def test_local_backend_never_hands_the_wrapper_to_residuation():
    backend = _local_backend(MagicMock(name="SmartMemory"))
    captured = {}

    class _FakeResiduation:
        def __init__(self, mem, **kw):
            captured["mem"] = mem

        def try_activate(self, decision_id):
            return True

    with patch(
        "smartmemory.reasoning.residuation.ResiduationManager", _FakeResiduation
    ):
        assert backend.decision_try_activate("dec_1") is True

    assert captured["mem"] is backend._mem
