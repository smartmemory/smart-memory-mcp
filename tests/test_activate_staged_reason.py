"""BUG-MCP-ACTIVATE-STAGED-REASON-1: ``decision_try_activate`` says a staged decision waits for its approval's publish.

Frozen notes ``smart-memory-ops/units/BUG-MCP-ACTIVATE-STAGED-REASON-1/notes-frozen.md`` rows A4, A4n, A1, A5, A9, which
close BUG-MCP-ACTIVATE-REPLY-1 note A4. Remote: the service reply recorded by the same transport double as
``test_decision_tools_remote_backend.py``. Local: a real core Lite store under ``tmp_path`` with a staged row written by
core's own staged-store writer, read through the real ``ResiduationManager``.
"""

from __future__ import annotations

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from tests.test_decision_tools_remote_backend import _remote, _run

STAGED = "Decision {id} waits for its approval's publish; it was not activated"
TODAY = "Decision {id} still pending (unresolved requirements remain)"
ACTIVATED = "Decision {id} activated (pending -> active)"


# ---------------------------------------------------------------- remote


def test_a4_remote_staged_reason_gets_the_staged_wording(monkeypatch):
    backend, transport = _remote({"activated": False, "decision": None, "reason": "staged"})
    out = _run("decision_try_activate", backend, monkeypatch, decision_id="dec_s")
    assert out == STAGED.format(id="dec_s")
    assert [c[:2] for c in transport.calls] == [("POST", "/memory/decisions/pending/dec_s/activate")]  # one call


def test_a4n_staged_but_hidden_reads_exactly_as_missing(monkeypatch):
    """The route answers a hidden staged id with the missing-id body, so the replies are byte-equal."""
    body = {"activated": False, "decision": None}
    hidden_backend, _ = _remote(dict(body))
    missing_backend, _ = _remote(dict(body))
    hidden = _run("decision_try_activate", hidden_backend, monkeypatch, decision_id="dec_x")
    missing = _run("decision_try_activate", missing_backend, monkeypatch, decision_id="dec_x")
    assert hidden == missing == TODAY.format(id="dec_x")  # A1: today's text on mcp main, unchanged


def test_a5_success_is_unchanged(monkeypatch):
    backend, _ = _remote({"activated": True, "decision": {"decision_id": "dec_p", "status": "active"}})
    assert _run("decision_try_activate", backend, monkeypatch, decision_id="dec_p") == ACTIVATED.format(id="dec_p")


@pytest.mark.parametrize("reason", ["published_later", "", 1, None, ["staged"]])
def test_a9_an_unknown_reason_is_no_reason(monkeypatch, reason):
    backend, _ = _remote({"activated": False, "decision": None, "reason": reason})
    assert _run("decision_try_activate", backend, monkeypatch, decision_id="dec_u") == TODAY.format(id="dec_u")


def test_the_bool_api_is_unchanged(monkeypatch):
    backend, _ = _remote({"activated": False, "decision": None, "reason": "staged"})
    assert backend.decision_try_activate("dec_s") is False
    assert backend.decision_activate("dec_s") == {"activated": False, "reason": "staged"}


# ---------------------------------------------------------------- local, real core


@pytest.fixture
def lite(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "")
    from smartmemory.tools.factory import create_lite_memory

    memory = create_lite_memory(data_dir=str(tmp_path / "mcp_staged_reason"), spawn_worker=False)
    try:
        yield memory
    finally:
        memory.close()


def _local(mem):
    backend = LocalBackend.__new__(LocalBackend)  # as test_pending_decision_tools_local_backend._local_backend
    backend._mem = mem
    return backend


def test_a4_local_staged_gets_the_staged_wording_and_missing_does_not(lite, monkeypatch):
    from smartmemory.decisions import staged_store

    properties = {"content": "Use camelCase.", "memory_type": "decision", "decision_type": "policy", "status": "active"}
    staged_store.put(lite._graph.backend, "dec_local_staged", "review_local_1", "decision", properties)
    backend = _local(lite)
    assert _run("decision_try_activate", backend, monkeypatch, decision_id="dec_local_staged") == STAGED.format(
        id="dec_local_staged"
    )
    assert _run("decision_try_activate", backend, monkeypatch, decision_id="dec_local_missing") == TODAY.format(
        id="dec_local_missing"
    )
    assert lite._graph.backend.get_node("dec_local_staged") is None  # nothing activated or published
