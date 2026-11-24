"""BUG-MCP-ACTIVATE-STAGED-REASON-1: ``decision_try_activate`` says a staged decision waits for its approval's publish.

Frozen notes ``smart-memory-ops/units/BUG-MCP-ACTIVATE-STAGED-REASON-1/notes-frozen.md`` rows A4, A4n, A1, A5, A9, which
close BUG-MCP-ACTIVATE-REPLY-1 note A4. Remote: the service reply recorded by the same transport double as
``test_decision_tools_remote_backend.py``. Local: a real core Lite store under ``tmp_path`` with a staged row written by
core's own staged-store writer, read through the real ``ResiduationManager``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from tests.test_decision_tools_remote_backend import _remote, _run

STAGED = "Decision {id} waits for its approval's publish; it was not activated"
TODAY = "Decision {id} still pending (unresolved requirements remain)"
ACTIVATED = "Decision {id} activated (pending -> active)"


# ---------------------------------------------------------------- remote


def test_a4_remote_staged_reason_gets_the_staged_wording(monkeypatch):
    backend, transport = _remote(
        {"activated": False, "decision": None, "reason": "staged"}
    )
    out = _run("decision_try_activate", backend, monkeypatch, decision_id="dec_s")
    assert out == STAGED.format(id="dec_s")
    assert [c[:2] for c in transport.calls] == [
        ("POST", "/memory/decisions/pending/dec_s/activate")
    ]  # one call


def test_a4n_staged_but_hidden_reads_exactly_as_missing(monkeypatch):
    """The route answers a hidden staged id with the missing-id body, so the replies are byte-equal."""
    body = {"activated": False, "decision": None}
    hidden_backend, _ = _remote(dict(body))
    missing_backend, _ = _remote(dict(body))
    hidden = _run(
        "decision_try_activate", hidden_backend, monkeypatch, decision_id="dec_x"
    )
    missing = _run(
        "decision_try_activate", missing_backend, monkeypatch, decision_id="dec_x"
    )
    assert (
        hidden == missing == TODAY.format(id="dec_x")
    )  # A1: today's text on mcp main, unchanged


def test_a5_success_is_unchanged(monkeypatch):
    backend, _ = _remote(
        {"activated": True, "decision": {"decision_id": "dec_p", "status": "active"}}
    )
    assert _run(
        "decision_try_activate", backend, monkeypatch, decision_id="dec_p"
    ) == ACTIVATED.format(id="dec_p")


@pytest.mark.parametrize("reason", ["published_later", "", 1, None, ["staged"]])
def test_a9_an_unknown_reason_is_no_reason(monkeypatch, reason):
    backend, _ = _remote({"activated": False, "decision": None, "reason": reason})
    assert _run(
        "decision_try_activate", backend, monkeypatch, decision_id="dec_u"
    ) == TODAY.format(id="dec_u")


def test_the_bool_api_is_unchanged(monkeypatch):
    backend, _ = _remote({"activated": False, "decision": None, "reason": "staged"})
    assert backend.decision_try_activate("dec_s") is False
    assert backend.decision_activate("dec_s") == {
        "activated": False,
        "reason": "staged",
    }


# ---------------------------------------------------------------- local, real core


@pytest.fixture
def lite(tmp_path, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    profile = PipelineConfig.lite(workspace_id="mcp_staged_ws", llm_enabled=False)
    profile.enrich.enricher_names = []
    memory = create_lite_memory(
        data_dir=str(tmp_path / "mcp_staged_reason"),
        pipeline_profile=profile,
        spawn_worker=False,
    )
    memory._reranker_config.enabled = False
    monkeypatch.setattr(
        memory.scope_provider,
        "get_workspace_supersession_policy",
        lambda: {"is_personal": False, "supersession_policy": "propose"},
        raising=False,
    )
    try:
        yield memory
    finally:
        memory.close()


def _local(mem):
    backend = LocalBackend.__new__(
        LocalBackend
    )  # as test_pending_decision_tools_local_backend._local_backend
    backend._mem = mem
    return backend


class _Stop(BaseException):
    """The process stops at the publish entry (core's own test hook, ``held_supersession._publish``)."""


def _approved_staged(mem, monkeypatch) -> str:
    """Core's real approve flow stopped at the publish (as core ``test_approval_f_staged._stopped``): an active rule,
    a held replacement, the review approved (``keep_b``), the replacement staged and never published. Fix r1 (review
    MEDIUM 1): "staged" is said only for an approved review's staged row, so the build's hand-written staged row with
    no review is replaced by this."""
    from smartmemory.decisions import held_supersession, staged_store
    from smartmemory.decisions.manager import DecisionManager
    from smartmemory.models.decision import Decision

    dm = DecisionManager(mem)
    d1 = dm.create(
        "All public API responses use snake_case field names.", decision_type="policy"
    ).decision_id
    held = dm.supersede(
        d1,
        Decision(content="Use camelCase.", decision_type="policy", confidence=0.9),
        "asked",
    )
    assert held.status == "held_for_review"

    def stop(*args, **kwargs):
        raise _Stop()

    with monkeypatch.context() as patch:
        patch.setattr(held_supersession, "_publish", stop)
        with pytest.raises(_Stop):
            mem.settle_conflict_review(
                held.review_id, action="keep_b", reason="ok", actor="human:alice"
            )
    assert staged_store.get(mem._graph.backend, held.new_decision_id) is not None
    assert mem._graph.backend.get_node(held.new_decision_id) is None
    return held.new_decision_id


def test_a4_local_staged_gets_the_staged_wording_and_missing_does_not(
    lite, monkeypatch
):
    staged_id = _approved_staged(lite, monkeypatch)
    backend = _local(lite)
    assert _run(
        "decision_try_activate", backend, monkeypatch, decision_id=staged_id
    ) == STAGED.format(id=staged_id)
    assert _run(
        "decision_try_activate", backend, monkeypatch, decision_id="dec_local_missing"
    ) == TODAY.format(id="dec_local_missing")
    assert (
        lite._graph.backend.get_node(staged_id) is None
    )  # nothing activated or published


# ---------------------------------------------------------------- fix r1, HIGH 2 (notes-frozen-r1.md H2-a..H2-c)


class _Cursor:
    def __init__(self, cursor, entry):
        self._cursor, self._entry = cursor, entry

    def _keep(self, rows):
        self._entry["rows"].extend([list(r) for r in rows])
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None:
            self._keep([row])
        return row

    def fetchall(self):
        return self._keep(self._cursor.fetchall())

    def fetchmany(self, *args):
        return self._keep(self._cursor.fetchmany(*args))

    def __iter__(self):
        for row in self._cursor:
            self._keep([row])
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _Connection:
    """The Lite store connection, recording each statement, its parameters and the rows it handed back."""

    def __init__(self, conn, log):
        self._real, self._log = conn, log

    def execute(self, sql, parameters=()):
        entry = {"sql": sql, "params": list(parameters), "rows": []}
        self._log.append(entry)
        return _Cursor(self._real.execute(sql, parameters), entry)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _reader(**kwargs):
    from smartmemory.scope_provider import DefaultScopeProvider

    hidden = kwargs.pop("hidden", False)

    class Reader(DefaultScopeProvider):
        def is_workspace_hidden(
            self,
        ) -> bool:  # core's workspace-hidden probe, the host's answer
            return hidden

    return Reader(**kwargs)


@contextmanager
def _as(mem, provider):
    """Read as ``provider`` on the same store: the memory's and its backend's provider, as a host binds them."""
    backend = mem._graph.backend
    before = (
        mem.scope_provider,
        getattr(backend, "scope_provider", None),
        hasattr(backend, "scope_provider"),
    )
    mem.scope_provider = provider
    backend.scope_provider = provider
    try:
        yield
    finally:
        mem.scope_provider = before[0]
        if before[2]:
            backend.scope_provider = before[1]
        else:
            del backend.scope_provider


def _activate_recorded(mem, reader, decision_id, monkeypatch):
    """The whole local call (backend ``decision_activate``) and the tool's reply, as ``reader``; every store read."""
    backend, log = _local(mem), []
    store = mem._graph.backend
    real = store._write_conn
    with _as(mem, reader):
        store._conn = _Connection(real, log)
        try:
            outcome = backend.decision_activate(decision_id)
        finally:
            store._conn = real
        reply = _run(
            "decision_try_activate", backend, monkeypatch, decision_id=decision_id
        )
    return outcome, log, reply


def _shape(log, *ids):
    def norm(value):
        text = json.dumps(value, sort_keys=True, default=str)
        for item_id in ids:
            text = text.replace(item_id, "<ID>")
        return text

    return [(norm(e["sql"]), norm(e["params"])) for e in log]


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(
            {"single_tenant": True, "hidden": True}, id="h2_a_hidden_workspace"
        ),
        pytest.param(
            {"workspace_id": "ws_b", "tenant_id": "t_b", "user_id": "u_b"},
            id="h2_b_another_workspace",
        ),
    ],
)
def test_h2_a_hidden_staged_and_missing_do_the_same_reads_through_the_whole_local_call(
    lite, monkeypatch, reader
):
    staged_id = _approved_staged(lite, monkeypatch)
    missing_id = "dec_local_missing_r1"
    hidden, hidden_log, hidden_reply = _activate_recorded(
        lite, _reader(**reader), staged_id, monkeypatch
    )
    missing, missing_log, missing_reply = _activate_recorded(
        lite, _reader(**reader), missing_id, monkeypatch
    )
    assert hidden == missing == {"activated": False, "reason": None}
    assert hidden_reply.replace(staged_id, "<ID>") == missing_reply.replace(
        missing_id, "<ID>"
    )
    assert any(
        "staged_nodes" not in e["sql"] for e in hidden_log
    )  # the item read is in the recorded call
    assert _shape(hidden_log, staged_id, missing_id) == _shape(
        missing_log, staged_id, missing_id
    )
    assert "camelCase" not in json.dumps(
        [e["rows"] for e in hidden_log], default=str
    )  # no staged content fetched


def test_h2_c_the_owner_keeps_the_bool_api_and_is_told_staged(lite, monkeypatch):
    from smartmemory.decisions import staged_store

    staged_id = _approved_staged(lite, monkeypatch)
    backend = _local(lite)
    before = staged_store.get(lite._graph.backend, staged_id)
    assert backend.decision_try_activate(staged_id) is False
    assert backend.decision_activate(staged_id) == {
        "activated": False,
        "reason": "staged",
    }
    assert staged_store.get(lite._graph.backend, staged_id) == before
    assert lite._graph.backend.get_node(staged_id) is None
