from unittest.mock import MagicMock, patch

from smartmemory_mcp.backends.local import LocalBackend


def _local_backend(mem=None):
    """A LocalBackend without running its smartmemory-importing __init__."""
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = mem if mem is not None else MagicMock(name="SmartMemory")
    return backend


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools():
    from smartmemory_mcp.tools import decision_tools

    mcp = _FakeMCP()
    decision_tools.register(mcp)
    return mcp.tools


def test_create_pending_builds_residuation_with_local_sm():
    tools = _tools()

    backend = _local_backend()
    captured = {}

    class _FakeDecision:
        """Stands in for a core Decision: the backend serializes via to_dict()."""

        def to_dict(self):
            return {
                "decision_id": "dec_pending1",
                "status": "pending",
                "pending_requirements": [
                    {
                        "requirement_id": "req_abc12345",
                        "description": "x",
                        "requirement_type": "proof",
                        "resolved": False,
                    }
                ],
            }

    class _FakeResiduation:
        def __init__(self, mem, **kw):
            captured["mem"] = mem

        def create_pending(self, **kw):
            captured["kw"] = kw
            return _FakeDecision()

    with (
        patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb,
        patch("smartmemory.reasoning.residuation.ResiduationManager", _FakeResiduation),
    ):
        gb.return_value = backend
        out = tools["decision_create_pending"](
            content="case",
            requirements=[{"description": "x", "requirement_type": "proof"}],
            domain="gtm",
        )

    assert captured["mem"] is backend._mem, (
        "ResiduationManager must be built with backend._mem"
    )
    assert "dec_pending1" in out
    assert "req_abc12345" in out


def test_resolve_requirement_calls_residuation():
    tools = _tools()

    backend = _local_backend()
    captured = {}

    class _FakeResiduation:
        def __init__(self, mem, **kw):
            captured["mem"] = mem

        def resolve_requirement(self, decision_id, requirement_id, memory_id):
            captured["args"] = (decision_id, requirement_id, memory_id)
            return True

    with (
        patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb,
        patch("smartmemory.reasoning.residuation.ResiduationManager", _FakeResiduation),
    ):
        gb.return_value = backend
        out = tools["decision_resolve_requirement"](
            decision_id="dec_123", requirement_id="req_456", memory_id="mem_789"
        )

    assert captured["mem"] is backend._mem
    assert captured["args"] == ("dec_123", "req_456", "mem_789")
    assert "req_456" in out
    assert "resolved" in out.lower()


def test_try_activate_calls_residuation():
    tools = _tools()

    backend = _local_backend()
    captured = {}

    class _FakeResiduation:
        def __init__(self, mem, **kw):
            captured["mem"] = mem

        def try_activate(self, decision_id):
            captured["decision_id"] = decision_id
            return True

    with (
        patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb,
        patch("smartmemory.reasoning.residuation.ResiduationManager", _FakeResiduation),
    ):
        gb.return_value = backend
        out = tools["decision_try_activate"](decision_id="dec_123")

    assert captured["mem"] is backend._mem
    assert captured["decision_id"] == "dec_123"
    assert "dec_123" in out
    assert "activated" in out.lower()
