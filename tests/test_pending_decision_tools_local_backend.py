from unittest.mock import MagicMock, patch


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


def test_create_pending_refuses_in_remote_mode():
    tools = _tools()
    with patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb:
        gb.return_value = object()  # no _mem -> remote
        out = tools["decision_create_pending"](
            content="case",
            requirements=[{"description": "x", "requirement_type": "proof"}],
        )
    assert isinstance(out, str)
    assert "local backend" in out.lower()


def test_create_pending_builds_residuation_with_local_sm():
    tools = _tools()

    class _LocalBackend:
        def __init__(self):
            self._mem = MagicMock(name="SmartMemory")

    backend = _LocalBackend()
    captured = {}

    class _FakeReq:
        requirement_id = "req_abc12345"
        description = "x"
        requirement_type = "proof"
        resolved = False

    class _FakeDecision:
        decision_id = "dec_pending1"
        status = "pending"
        pending_requirements = [_FakeReq()]

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


def test_resolve_requirement_refuses_in_remote_mode():
    tools = _tools()
    with patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb:
        gb.return_value = object()  # no _mem -> remote
        out = tools["decision_resolve_requirement"](
            decision_id="dec_test", requirement_id="req_test", memory_id="mem_test"
        )
    assert isinstance(out, str)
    assert "local backend" in out.lower()


def test_resolve_requirement_calls_residuation():
    tools = _tools()

    class _LocalBackend:
        def __init__(self):
            self._mem = MagicMock(name="SmartMemory")

    backend = _LocalBackend()
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


def test_try_activate_refuses_in_remote_mode():
    tools = _tools()
    with patch("smartmemory_mcp.tools.decision_tools.get_backend") as gb:
        gb.return_value = object()  # no _mem -> remote
        out = tools["decision_try_activate"](decision_id="dec_test")
    assert isinstance(out, str)
    assert "local backend" in out.lower()


def test_try_activate_calls_residuation():
    tools = _tools()

    class _LocalBackend:
        def __init__(self):
            self._mem = MagicMock(name="SmartMemory")

    backend = _LocalBackend()
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
