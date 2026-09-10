"""MCP-REMOTE-DECISIONS-1 contract — rendered output is identical in both modes.

The acceptance criterion is not "remote works" but "remote reads the same". An
agent's prompt is the rendered string, so a divergence in wording, ordering or a
dropped field between local and remote mode is a behaviour change the agent sees
even though both calls succeeded.

Each case drives ONE tool twice — once through ``LocalBackend`` over faked core
managers, once through ``RemoteBackend`` over a faked httpx transport returning the
service's real response envelope — and asserts the two strings are equal.
"""

from unittest.mock import patch

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend


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


DECISION = {
    "decision_id": "dec_1",
    "content": "Use Postgres for the ledger",
    "decision_type": "choice",
    "confidence": 0.7,
    "status": "active",
    "rejected_alternatives": ["DynamoDB"],
    "rationale": "ACID and team familiarity",
    "constraints": ["single region"],
    "pending_requirements": [],
    "ignorance": 1.0,
}

PENDING = {
    "decision_id": "dec_p",
    "content": "Ship the ledger",
    "status": "pending",
    "pending_requirements": [
        {
            "requirement_id": "req_abc12345",
            "description": "load test passes",
            "requirement_type": "proof",
            "resolved": False,
        }
    ],
}


class _Decision:
    """A core Decision stand-in: the local backend serializes it with to_dict()."""

    def __init__(self, payload):
        self._payload = payload
        for key, value in payload.items():
            setattr(self, key, value)

    def to_dict(self):
        return dict(self._payload)


def _local(**core):
    """LocalBackend whose core managers are replaced by the given fakes."""
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = object()
    for name, factory in core.items():
        setattr(backend, name, factory)
    return backend


def _remote(response):
    backend = RemoteBackend.__new__(RemoteBackend)
    backend._request = lambda method, path, **kwargs: response
    return backend


def _render(tool, backend, **kwargs):
    with patch("smartmemory_mcp.tools.decision_tools.get_backend", lambda: backend):
        return _tools()[tool](**kwargs)


class _Manager:
    def __init__(self, **behaviour):
        for name, value in behaviour.items():
            setattr(self, name, value)


def _cases():
    decision = _Decision(DECISION)
    pending = _Decision(PENDING)

    yield (
        "decision_create",
        {
            "content": "Use Postgres for the ledger",
            "decision_type": "choice",
            "confidence": 0.7,
        },
        _local(_decision_manager=lambda: _Manager(create=lambda **kw: decision)),
        _remote({**DECISION, "user_id": "u", "workspace_id": "w", "tenant_id": "t"}),
    )
    yield (
        "decision_get",
        {"decision_id": "dec_1"},
        _local(_decision_manager=lambda: _Manager(get_decision=lambda _id: decision)),
        _remote(DECISION),
    )
    yield (
        "decision_list",
        {},
        _local(
            _decision_queries=lambda: _Manager(
                get_active_decisions=lambda **kw: [decision]
            )
        ),
        _remote({"decisions": [DECISION], "count": 1}),
    )
    yield (
        "decision_search",
        {"topic": "ledger"},
        _local(
            _decision_queries=lambda: _Manager(
                get_decisions_about=lambda **kw: [decision]
            )
        ),
        _remote({"decisions": [DECISION], "count": 1, "topic": "ledger"}),
    )
    yield (
        "decision_supersede",
        {
            "decision_id": "dec_1",
            "new_content": "Use CockroachDB",
            "reason": "multi-region",
            "new_confidence": 0.95,
            "rejected_alternatives": ["Keep Citus"],
            "rationale": "Regional failover is required",
            "constraints": ["No manual operator"],
        },
        _local(
            _decision_manager=lambda: _Manager(
                supersede=lambda _id, _new, reason, **context: _Decision(
                    {**DECISION, "decision_id": "dec_2"}
                )
            )
        ),
        _remote(
            {
                "old_decision_id": "dec_1",
                "new_decision_id": "dec_2",
                "status": "superseded",
                "user_id": "u",
            }
        ),
    )
    yield (
        "decision_retract",
        {"decision_id": "dec_1", "reason": "wrong"},
        _local(_decision_manager=lambda: _Manager(retract=lambda _id, reason: None)),
        _remote({"decision_id": "dec_1", "status": "retracted", "user_id": "u"}),
    )
    yield (
        "decision_reinforce",
        {"decision_id": "dec_1", "evidence_id": "mem_1"},
        _local(
            _decision_manager=lambda: _Manager(
                reinforce=lambda _id, _ev: _Decision(
                    {**DECISION, "confidence": 0.955, "reinforcement_count": 1}
                )
            )
        ),
        _remote(
            {"decision_id": "dec_1", "confidence": 0.955, "reinforcement_count": 1}
        ),
    )
    yield (
        "decision_contradict",
        {"decision_id": "dec_1", "evidence_id": "mem_1"},
        _local(
            _decision_manager=lambda: _Manager(
                contradict=lambda _id, _ev: _Decision(
                    {**DECISION, "confidence": 0.81, "contradiction_count": 2}
                )
            )
        ),
        _remote({"decision_id": "dec_1", "confidence": 0.81, "contradiction_count": 2}),
    )
    yield (
        "decision_provenance",
        {"decision_id": "dec_1"},
        _local(
            _decision_queries=lambda: _Manager(
                get_decision_provenance=lambda _id: {
                    "decision": decision,
                    "reasoning_trace": None,
                    "evidence": [{"memory": {}}],
                    "superseded": [decision],
                    "superseded_by": [],
                }
            )
        ),
        _remote(
            {
                "decision": DECISION,
                "reasoning_trace": None,
                "evidence": [{"memory": {}}],
                "superseded": [DECISION],
                "superseded_by": [],
            }
        ),
    )
    yield (
        "decision_find_conflicts",
        {"decision_id": "dec_1"},
        _local(
            _decision_manager=lambda: _Manager(
                get_decision=lambda _id: decision,
                find_conflicts=lambda _d: [decision],
            )
        ),
        _remote({"decision_id": "dec_1", "conflicts": [DECISION], "count": 1}),
    )
    yield (
        "decision_create_pending",
        {
            "content": "Ship the ledger",
            "requirements": [
                {"description": "load test passes", "requirement_type": "proof"}
            ],
        },
        _local(_residuation=lambda: _Manager(create_pending=lambda **kw: pending)),
        _remote(PENDING),
    )
    yield (
        "decision_resolve_requirement",
        {"decision_id": "dec_p", "requirement_id": "req_1", "memory_id": "mem_1"},
        _local(_residuation=lambda: _Manager(resolve_requirement=lambda *a: True)),
        _remote({"resolved": True, "decision": None}),
    )
    yield (
        "decision_try_activate",
        {"decision_id": "dec_p"},
        _local(_residuation=lambda: _Manager(try_activate=lambda _id: True)),
        _remote({"activated": True, "decision": None}),
    )


CASES = list(_cases())


@pytest.mark.parametrize("tool,kwargs,local,remote", CASES, ids=[c[0] for c in CASES])
def test_rendered_output_is_identical_in_both_modes(tool, kwargs, local, remote):
    assert _render(tool, local, **kwargs) == _render(tool, remote, **kwargs)


def test_the_parity_suite_covers_every_registered_decision_tool():
    """A new decision tool must arrive with its own parity case, not silently skip it."""
    registered = {name for name in _tools() if name.startswith("decision_")}
    covered = {case[0] for case in CASES}
    assert registered == covered, f"uncovered decision tools: {registered - covered}"


@pytest.mark.parametrize(
    "tool,kwargs,local,remote",
    [
        (
            "decision_get",
            {"decision_id": "dec_x"},
            _local(_decision_manager=lambda: _Manager(get_decision=lambda _id: None)),
            _remote({"error": "API error 404: Decision not found"}),
        ),
        (
            "decision_find_conflicts",
            {"decision_id": "dec_x"},
            _local(_decision_manager=lambda: _Manager(get_decision=lambda _id: None)),
            _remote({"error": "API error 404: Decision not found"}),
        ),
    ],
    ids=["decision_get", "decision_find_conflicts"],
)
def test_absence_reads_the_same_in_both_modes(tool, kwargs, local, remote):
    assert _render(tool, local, **kwargs) == _render(tool, remote, **kwargs)
