"""BUG-MCP-ACTIVATE-REPLY-1: decision_try_activate says why it did not activate (notes A1-A8; A4 staged is blocked)."""

from unittest.mock import patch

from smartmemory_mcp.tools import decision_tools

OLD = "Decision dec_1 still pending (unresolved requirements remain)"


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Backend:
    def __init__(self, activated, row=None, raises=False):
        self.activated, self.row, self.raises = activated, row, raises
        self.activate_calls, self.get_calls = [], []

    def decision_try_activate(self, decision_id):
        self.activate_calls.append(decision_id)
        return self.activated

    def decision_get(self, decision_id, **kw):
        self.get_calls.append((decision_id, kw))
        if self.raises:
            raise RuntimeError("read failed")
        return self.row


def _run(backend):
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    with patch("smartmemory_mcp.tools.decision_tools.get_backend", return_value=backend):
        return mcp.tools["decision_try_activate"](decision_id="dec_1")


def _req(desc, resolved=False, **kw):
    return {"requirement_id": "r_internal", "description": desc, "resolved": resolved, **kw}


def test_a1_missing_id_says_not_found():
    assert _run(_Backend(False, None)) == "Decision dec_1 not found"


def test_a2_already_active_says_so():
    assert _run(_Backend(False, {"decision_id": "dec_1", "status": "active"})) == "Decision dec_1 is already active"


def test_a3_pending_names_open_requirements_only():
    row = {"status": "pending", "pending_requirements": [_req("Wall-load check"), _req("Facilities sign-off", True)]}
    out = _run(_Backend(False, row))
    assert "still pending" in out and "Wall-load check" in out
    assert "Facilities sign-off" not in out and "r_internal" not in out


def test_a5_success_text_unchanged_and_no_read():
    backend = _Backend(True, {"status": "active"})
    assert _run(backend) == "Decision dec_1 activated (pending -> active)"
    assert backend.get_calls == []


def test_a6_unreadable_requirement_is_counted_not_named():
    row = {"status": "pending", "pending_requirements": [
        _req("Wall-load check"), _req("Secret budget review", visible=False), _req("Hidden legal check", visible=False)]}
    out = _run(_Backend(False, row))
    assert "Wall-load check" in out and "and 2 more you cannot see" in out
    assert "Secret budget review" not in out and "Hidden legal check" not in out


def test_a7_read_failure_and_no_open_requirement_keep_the_old_text():
    assert _run(_Backend(False, raises=True)) == OLD
    assert _run(_Backend(False, {"status": "pending", "pending_requirements": []})) == OLD
    assert _run(_Backend(False, {"status": "retracted"})) == OLD


def test_a8_activation_called_once_and_read_is_plain():
    backend = _Backend(False, {"status": "pending", "pending_requirements": [_req("Check")]})
    _run(backend)
    assert backend.activate_calls == ["dec_1"]
    assert backend.get_calls == [("dec_1", {})]
