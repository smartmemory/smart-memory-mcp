"""[bug-hunt 2026-06-02 — CRITICAL] decision MCP tools must use the local
SmartMemory (``backend._mem``), not the MCP backend wrapper.

Passing the wrapper made the ManagedType framework call ``LocalBackend.add(memory_item)``
— whose signature is ``add(content: str, ...)`` — so the MemoryItem was received as
``content`` and silently re-wrapped: decision_type/confidence/rationale/item_id all
discarded, stored as a semantic node, unreadable afterward. The tool reported success
while writing garbage. Fix: pass ``backend._mem``; in remote mode (no ``_mem``) refuse
with a clear message instead of corrupting data.
"""

from unittest.mock import MagicMock, patch


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


class _LocalBackend:
    _mem = object()  # stands in for the real SmartMemory in local mode


def test_decision_create_refuses_in_remote_mode():
    """Remote backend has no `_mem` -> clear refusal, NOT silent corruption."""
    tools = _tools()
    with patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb:
        gb.return_value = object()  # no _mem
        out = tools["decision_create"](
            content="Use JWT", decision_type="preference", confidence=0.9
        )
    assert isinstance(out, str)
    assert "local backend" in out.lower(), (
        f"expected a local-backend refusal, got: {out!r}"
    )


def test_decision_create_builds_manager_with_local_sm_not_wrapper():
    """Forcing function: DecisionManager is constructed with backend._mem (the
    real SmartMemory), never the MCP wrapper that would corrupt the write."""
    tools = _tools()
    backend = _LocalBackend()
    captured = {}

    class _FakeManager:
        def __init__(self, mem):
            captured["mem"] = mem

        def create(self, **kw):
            d = MagicMock()
            d.decision_id = "dec_abc123"
            d.decision_type = kw.get("decision_type")
            d.confidence = kw.get("confidence")
            return d

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


def test_decision_query_refuses_in_remote_mode():
    """A read tool (decision_list) must also refuse remote, not silently return []."""
    tools = _tools()
    with patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb:
        gb.return_value = object()
        out = tools["decision_list"]()
    assert isinstance(out, str)
    assert "local backend" in out.lower()
