"""BUG-MCP-DECISION-GET-RAW-1: decision_get answers in readable lines (notes G1-G8)."""

from unittest.mock import patch

from smartmemory.models.decision import Decision
from smartmemory.models.pending_requirement import PendingRequirement

from smartmemory_mcp.tools import decision_tools

AS_OF = "2020-01-15T00:00:00Z"
FILLER = "x" * 40


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Backend:
    def __init__(self, row):
        self.row = row

    def decision_get(self, decision_id, **kw):
        return self.row


def _get(row, **kw):
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    with patch("smartmemory_mcp.tools.decision_tools.get_backend", return_value=_Backend(row)):
        return mcp.tools["decision_get"](decision_id="dec_1", **kw)


def _active():
    return Decision(
        decision_id="dec_1",
        content="Blue case for the kiosk",
        confidence=0.9,
        status="active",
        rationale="It is easier for staff to spot.",
        recorded_by="user-a",
        evidence_ids=["ev_internal_1"],
        source_trace_id="trace_internal_1",
        agent_id="agent_internal_1",
    ).to_dict()


def _retracted():
    d = _active()
    d.update(status="retracted", content="Orange case for the kiosk")
    d["context_snapshot"] = {
        "retraction_reason": "The choice was reopened.",
        "retracted_at": "2026-11-23T10:00:00+00:00",
        "internal_note": FILLER,
    }
    return d


def _pending():
    d = _active()
    d.update(status="pending", content="Wall mount for the kiosk")
    d["pending_requirements"] = [
        PendingRequirement(requirement_id="r1", description="Wall-load check", requirement_type="evidence").to_dict(),
        PendingRequirement(
            requirement_id="r2", description="Facilities sign-off", requirement_type="confirmation",
            resolved=True, resolved_by="user-b", resolved_at=None,
        ).to_dict(),
    ]
    return d


def test_g1_active_statement_state_rationale_in_order_no_raw_repr():
    out = _get(_active())
    lines = out.splitlines()
    assert "Blue case for the kiosk" in lines[0]
    assert lines[1].startswith("State: active")
    assert lines[2] == "Rationale: It is easier for staff to spot."
    assert "{" not in out and "'" not in out.split("Recorded")[0]


def test_g2_retracted_reason_follows_rationale_before_attribution():
    lines = _get(_retracted()).splitlines()
    assert lines[1].startswith("State: retracted")
    reason = next(i for i, x in enumerate(lines) if "The choice was reopened." in x)
    assert reason == 3
    assert reason < next(i for i, x in enumerate(lines) if x.startswith("Recorded by"))


def test_g3_pending_requirements_open_and_done_before_attribution():
    lines = _get(_pending()).splitlines()
    assert "State: pending" in lines[1]
    open_line = next(i for i, x in enumerate(lines) if "[open] Wall-load check" in x)
    done_line = next(i for i, x in enumerate(lines) if "[done] Facilities sign-off" in x and "supporting memory user-b" in x)
    assert open_line < next(i for i, x in enumerate(lines) if x.startswith("Recorded by"))
    assert done_line > open_line


def test_g4_attribution_and_dates_kept():
    d = _active()
    d.update(recorded_for="site-1", decided_by="user-c", created_at="2026-11-20T09:00:00+00:00")
    out = _get(d)
    assert "Recorded by: user-a for site-1" in out
    assert "Recorded on: 2026-11-20T09:00:00+00:00" in out
    assert "Decided by (as stated, not verified): user-c" in out


def test_g5_as_of_names_instant_and_undated_does_not():
    assert AS_OF in _get(_active(), as_of=AS_OF).splitlines()[0]
    assert "as of" not in _get(_active())


def test_g6_absent_fields_omitted_no_none():
    out = _get({"decision_id": "dec_9", "content": "Bare decision", "status": "active", "rationale": None,
                "pending_requirements": [], "context_snapshot": {}, "created_at": "2026-11-20T09:00:00+00:00"})
    assert "None" not in out
    assert "Rationale" not in out and "Requirements" not in out and "Retracted" not in out
    assert out.splitlines()[:2] == ["Decision dec_9: Bare decision", "State: active"]


def test_g7_nothing_beyond_the_authorized_payload():
    out = _get(_retracted())
    for hidden in ("ev_internal_1", "trace_internal_1", "agent_internal_1", FILLER, "reinforcement_count", "evidence_ids"):
        assert hidden not in out
    hidden_rule = _get({**_active(), "replaced": {"visible": False, "decision_id": "dec_secret", "at": "2026-11-21"}})
    assert "dec_secret" not in hidden_rule and "replaced by" in hidden_rule


def test_g8_reply_size_well_under_old_raw_reply():
    for row in (_active(), _retracted(), _pending()):
        out = _get(row)
        assert len(out) < 1000
        assert len(out) * 2 < len(str(row))  # the old reply was the raw object string plus the who lines


def test_g9_belief_reads_keep_their_names_and_are_omitted_when_not_returned():
    out = _get(_active())
    assert "Evidence: belief_hold=0.00 plausibility_hold=1.00 ignorance=1.00" in out
    assert "Evidence:" not in _get({"decision_id": "dec_9", "content": "Bare", "status": "active"})


def test_r1_m1_resolved_by_is_supporting_memory_not_an_actor():
    d = _active()
    d["pending_requirements"] = [
        PendingRequirement(requirement_id="r2", description="Facilities sign-off", requirement_type="evidence",
                           resolved=True, resolved_by="memory_evidence_123", resolved_at=None).to_dict()
    ]
    out = _get(d)
    assert "cleared by" not in out
    assert "supporting memory memory_evidence_123" in out


def test_r1_l1_withheld_statement_prints_no_none():
    row = {"decision_id": "dec_1", "content": None, "as_of_resolution": "unresolved",
           "as_of_unknown_fields": ["content"]}
    out = _get(row, as_of=AS_OF)
    assert out.splitlines()[0] == f"Decision dec_1 as of {AS_OF}"
    assert "None" not in out and "Not known at that instant: content" in out
    assert _get({"decision_id": "dec_2", "status": "active"}).splitlines()[0] == "Decision dec_2"


def test_r1_l2_unreturned_resolution_has_no_state_marker():
    out = _get({**_active(), "pending_requirements": [{"description": "Wall-load evidence"}]})
    assert "- Wall-load evidence" in out
    assert "[open]" not in out and "[done]" not in out
    out = _get({**_active(), "pending_requirements": [{"description": "A", "resolved": False}]})
    assert "- [open] A" in out
