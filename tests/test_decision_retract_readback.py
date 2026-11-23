"""BUG-MCP-RETRACT-ALT-READBACK-1: decision_retract reads back the alternative (notes R1-R6)."""

from unittest.mock import patch

import pytest

from smartmemory_mcp.tools import decision_tools

PLAIN = "Decision retracted: dec_1"


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Backend:
    def __init__(self, retract=None, alt=None, read_raises=False, retract_raises=False):
        self.retract = {"decision_id": "dec_1", "status": "retracted"} if retract is None else retract
        self.alt, self.read_raises, self.retract_raises = alt, read_raises, retract_raises
        self.retract_calls, self.get_calls = [], []

    def decision_retract(self, decision_id, **kw):
        self.retract_calls.append((decision_id, kw))
        if self.retract_raises:
            raise RuntimeError("retract failed")
        return self.retract or None

    def decision_get(self, decision_id, **kw):
        self.get_calls.append((decision_id, kw))
        if self.read_raises:
            raise RuntimeError("read failed")
        return self.alt


def _run(backend, **kw):
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    with patch("smartmemory_mcp.tools.decision_tools.get_backend", return_value=backend):
        return mcp.tools["decision_retract"](decision_id="dec_1", reason="reopened", **kw)


def _alt():
    return {"decision_id": "dec_2", "content": "Open on Saturdays", "status": "active"}


def test_r1_no_alternative_is_unchanged_and_reads_nothing():
    backend = _Backend()
    assert _run(backend) == PLAIN
    assert backend.get_calls == []


def test_r2_readable_alternative_is_read_back_in_one_reply():
    out = _run(_Backend(alt=_alt()), alternative_id="dec_2")
    assert out.splitlines()[0] == PLAIN
    assert "Open on Saturdays" in out and "State: active" in out


def test_r3_missing_or_unreadable_alternative_says_not_found_alike():
    for alt in (None, {}):
        out = _run(_Backend(alt=alt), alternative_id="dec_hidden")
        assert out == f"{PLAIN}\nAlternative dec_hidden not found"
    # not-found text is identical whether absent or hidden: nothing beyond the id the caller supplied
    assert "exists" not in out and "hidden" not in out.replace("dec_hidden", "")


def test_r4_failed_retraction_has_no_readback():
    backend = _Backend(retract={}, alt=_alt())
    assert _run(backend, alternative_id="dec_2") == "Decision not found: dec_1"
    assert backend.get_calls == []
    backend = _Backend(retract_raises=True, alt=_alt())
    with pytest.raises(RuntimeError, match="retract failed"):  # the error propagates as today
        _run(backend, alternative_id="dec_2")
    assert backend.get_calls == []


def test_r5_readback_failure_is_named_apart_from_the_retraction():
    out = _run(_Backend(read_raises=True), alternative_id="dec_2")
    assert out.splitlines()[0] == PLAIN
    assert "could not be read back" in out.splitlines()[1] and "dec_2" in out.splitlines()[1]


def test_r6_retract_call_unchanged_and_alternative_only_read():
    backend = _Backend(alt=_alt())
    _run(backend, alternative_id="dec_2")
    assert backend.retract_calls == [("dec_1", {"reason": "reopened"})]
    assert backend.get_calls == [("dec_2", {})]
