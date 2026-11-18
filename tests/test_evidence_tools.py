"""SM-EVIDENCE-TYPE-1: `evidence_record` and `decision_link_evidence` in local and remote mode.

Local: a real Lite store (``create_lite_memory(tmp_path, spawn_worker=False)``) behind ``LocalBackend._mem``. A Compose
receipt (``#/definitions/record`` of forge/compose ``contracts/evidence.schema.json`` v1.2.0, copied to
``tests/fixtures/compose-evidence.schema.json`` at compose 7c67cc70) is built here, never read from a live store; it
becomes one `evidence` node whose fields equal the record, and mapping it back validates against the schema. A decision
links to it with exactly one DERIVED_FROM role=evidence edge and no confidence change; refusals name their cause and
write no edge.

Remote: the two service routes (``POST /memory/evidence``, ``POST /memory/decisions/{id}/evidence``) with the exact body,
the decision id through ``path_guard.path_id``, and the route's 404/409/422 refusals read as the same named refusal as
local mode. The service side of the round trip (and the cross-workspace refusal) is pinned in smart-memory-service
``tests/integration/api/test_evidence.py`` and ``tests/security/test_decision_tenant_isolation.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend

REF = "ev:0123456789abcdef"
SCHEMA_PATH = Path(__file__).parent / "fixtures" / "compose-evidence.schema.json"


def compose_record(**over) -> dict:
    rec = {
        "id": REF,
        "evidence": {
            "kind": "sha",
            "repo": "/Users/dev/smart-memory-core",
            "sha": "afee2c429d3e",
            "branch": "2.0",
            "at": "2026-11-04T15:00:00+08:00",
            "by": "sm20-main",
        },
        "checked_at": "2026-11-04T07:00:05.123Z",
        "checks": ["commit afee2c429d3e exists", "ancestor of 2.0"],
        "subject": {"unit": "SM-EVIDENCE-TYPE-1", "claim": "LANDED"},
    }
    rec.update(over)
    return rec


def validate_compose_record(rec: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    schema.pop("oneOf", None)
    jsonschema.validate(rec, {**schema, "$ref": "#/definitions/record"})


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _call(backend, tool: str, **kwargs) -> str:
    from smartmemory_mcp.tools import decision_tools

    mcp = _FakeMCP()
    decision_tools.register(mcp)
    with patch("smartmemory_mcp.tools.decision_tools.get_backend", lambda: backend):
        return mcp.tools[tool](**kwargs)


# --- local mode, real Lite store -------------------------------------------------


@pytest.fixture
def local(tmp_path):
    pytest.importorskip("smartmemory.evidence")
    from smartmemory.tools.factory import create_lite_memory

    mem = create_lite_memory(str(tmp_path / "evidence"), spawn_worker=False)
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = mem
    try:
        yield backend
    finally:
        close = getattr(mem, "close", None)
        if close:
            close()


def _derived_from(mem, decision_id: str) -> list[dict]:
    return [
        e
        for e in mem._graph.backend.get_all_edges()
        if e.get("source_id") == decision_id and e.get("edge_type") == "DERIVED_FROM"
    ]


def _decision(backend, content: str, confidence: float = 0.7) -> str:
    return backend.decision_create(content, confidence=confidence)["decision_id"]


def test_headline_record_saved_locally_is_an_evidence_node_equal_to_the_record(local):
    from smartmemory.evidence import to_record

    rec = compose_record()
    validate_compose_record(rec)

    out = _call(local, "evidence_record", record=rec)
    assert out.startswith(f"Evidence recorded: {REF}"), out

    node = local._mem.get_evidence(REF)
    assert node is not None
    assert local._mem.get(node.evidence_id).memory_type == "evidence"
    assert (node.evidence_ref, node.kind, node.subject, node.checker) == (
        REF,
        "sha",
        rec["subject"],
        "sm20-main",
    )
    assert (node.observed_at, node.checked_at, node.checks, node.payload) == (
        rec["evidence"]["at"],
        rec["checked_at"],
        rec["checks"],
        rec["evidence"],
    )
    back = to_record(node)
    assert back == rec
    validate_compose_record(back)

    # Idempotent on the ref: the same record again is the same node, no second write.
    again = local.evidence_record(json.loads(json.dumps(rec)))
    assert again["evidence_id"] == node.evidence_id and again["record"] == rec


def test_link_writes_one_edge_records_the_id_once_and_keeps_confidence(local):
    ev = local.evidence_record(compose_record())
    decision_id = _decision(local, "evidence nodes ship in 2.0", confidence=0.7)

    out = _call(
        local, "decision_link_evidence", decision_id=decision_id, evidence_id=REF
    )
    assert (
        out.splitlines()[0] == f"Evidence linked: {ev['evidence_id']} -> {decision_id}"
    ), out
    assert "Confidence: 0.70 (unchanged)" in out
    second = local.decision_link_evidence(
        decision_id, ev["evidence_id"]
    )  # by node id: a no-op
    assert second["evidence_ids"] == [ev["evidence_id"]]

    decision = local._mem.get_decision(decision_id)
    assert decision.evidence_ids == [ev["evidence_id"]]
    assert decision.confidence == pytest.approx(0.7)
    edges = _derived_from(local._mem, decision_id)
    assert [e["target_id"] for e in edges] == [ev["evidence_id"]]
    props = edges[0].get("properties") or {}
    if isinstance(props, str):
        props = json.loads(props)
    assert props.get("role") == "evidence"


def test_provenance_names_the_receipt(local):
    local.evidence_record(compose_record())
    decision_id = _decision(local, "provenance names the receipt")
    local.decision_link_evidence(decision_id, REF)

    rows = local.decision_provenance(decision_id)["evidence"]
    assert [(r["evidence_ref"], r["kind"], r["checked_at"]) for r in rows] == [
        (REF, "sha", "2026-11-04T07:00:05.123Z")
    ]
    out = _call(local, "decision_provenance", decision_id=decision_id)
    assert f"- {REF} (sha, checked 2026-11-04T07:00:05.123Z)" in out


def test_link_refusals_name_their_cause_and_write_no_edge(local):
    local.evidence_record(compose_record())
    live = _decision(local, "needs proof")
    other = _decision(local, "i am a decision, not a receipt")
    ended = _decision(local, "will be retracted")
    local.decision_retract(ended, "wrong")

    rows = (
        (live, "ev:ffffffffffffffff", "Evidence not found: ev:ffffffffffffffff"),
        (live, other, "memory_type 'decision', not evidence"),
        (ended, REF, "status 'retracted'"),
        ("dec_missing", REF, "Decision not found"),
    )
    for decision_id, evidence_id, cause in rows:
        out = _call(
            local,
            "decision_link_evidence",
            decision_id=decision_id,
            evidence_id=evidence_id,
        )
        assert out.startswith("Evidence not linked: ") and cause in out, (
            decision_id,
            evidence_id,
            out,
        )
        assert _derived_from(local._mem, decision_id) == []


def test_malformed_record_is_refused_and_nothing_is_stored(local):
    out = _call(local, "evidence_record", record=compose_record(id="not-an-ev-ref"))
    assert out.startswith("Evidence not recorded: "), out
    assert local._mem.get_evidence("not-an-ev-ref") is None


# --- remote mode, the service routes ---------------------------------------------


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls: list = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


def _remote(response) -> tuple[RemoteBackend, _Transport]:
    backend = RemoteBackend.__new__(RemoteBackend)
    transport = _Transport(response)
    backend._request = transport
    return backend, transport


def _refusal(status: int, detail: str) -> dict:
    return {
        "error": f"API error {status}: {json.dumps({'detail': detail})}",
        "status_code": status,
        "body": {"detail": detail},
    }


EVIDENCE_RESPONSE = {
    "evidence_id": REF,
    "evidence_ref": REF,
    "kind": "sha",
    "subject": {"unit": "SM-EVIDENCE-TYPE-1", "claim": "LANDED"},
    "checker": "sm20-main",
    "observed_at": "2026-11-04T15:00:00+08:00",
    "checked_at": "2026-11-04T07:00:05.123Z",
    "checks": ["commit afee2c429d3e exists", "ancestor of 2.0"],
    "payload": compose_record()["evidence"],
    "record": compose_record(),
}


def test_remote_record_posts_the_record_to_the_evidence_route():
    backend, transport = _remote(EVIDENCE_RESPONSE)
    rec = compose_record()
    out = _call(backend, "evidence_record", record=rec)
    assert transport.calls == [("POST", "/memory/evidence", {"json": {"record": rec}})]
    assert out == (
        f"Evidence recorded: {REF}\nRef: {REF}\nKind: sha\nChecked at: 2026-11-04T07:00:05.123Z"
    )


def test_remote_link_posts_the_id_under_the_guarded_decision_path():
    backend, transport = _remote(
        {
            "decision_id": "dec_1",
            "evidence_id": REF,
            "evidence_ids": [REF],
            "confidence": 0.7,
        }
    )
    out = _call(backend, "decision_link_evidence", decision_id="dec_1", evidence_id=REF)
    assert transport.calls == [
        ("POST", "/memory/decisions/dec_1/evidence", {"json": {"evidence_id": REF}})
    ]
    assert (
        out
        == f"Evidence linked: {REF} -> dec_1\nEvidence count: 1\nConfidence: 0.70 (unchanged)"
    )

    with pytest.raises(ValueError, match="another route"):
        backend.decision_link_evidence("pending", REF)
    assert len(transport.calls) == 1


def test_remote_refusals_read_like_local_refusals():
    rows = (
        (404, "Evidence not found: ev:ffffffffffffffff"),
        (
            409,
            "Decision 'dec_1' is in status 'retracted'; only live decisions can take evidence",
        ),
        (422, "Node dec_2 is memory_type 'decision', not evidence"),
    )
    for status, detail in rows:
        backend, _transport = _remote(_refusal(status, detail))
        out = _call(
            backend, "decision_link_evidence", decision_id="dec_1", evidence_id="x"
        )
        assert out == f"Evidence not linked: {detail}"

    backend, _transport = _remote(
        _refusal(422, "Evidence record id must be ev: + 16 hex")
    )
    assert (
        _call(backend, "evidence_record", record={})
        == "Evidence not recorded: Evidence record id must be ev: + 16 hex"
    )


def test_remote_server_error_is_loud_not_a_refusal():
    backend, _transport = _remote(
        {"error": "API error 500: boom", "status_code": 500, "body": {"detail": "boom"}}
    )
    with pytest.raises(
        RuntimeError, match="POST /memory/evidence failed: API error 500"
    ):
        _call(backend, "evidence_record", record=compose_record())
