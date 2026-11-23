"""BUG-PROVENANCE-COUNTS-ONLY-1: the provenance tool text lists each row, not only counts.

One test per frozen note (units/BUG-PROVENANCE-COUNTS-ONLY-1/notes-frozen.md), N1..N13 (N8..N13 added in fix round 1; they use core's real row shape).
"""

from __future__ import annotations

from smartmemory_mcp.backends.remote import RemoteBackend

from tests.test_decision_tools_as_of import _tools, _use


def _link(decision_id, reason, at, visible=True):
    """Core's ReplacedLink (decisions/who_replaced.py `_link`)."""
    return {"decision_id": decision_id, "visible": visible, "at": at, "by": None, "reason": reason}


def _row(decision_id, content, created="2025-12-01T00:00:00+00:00", **snapshot):
    """A decision row as core's Decision.to_dict serves it: the supersession note lives in context_snapshot."""
    return {"decision_id": decision_id, "content": content, "created_at": created, "context_snapshot": snapshot}


# dec_1 is B in A(dec_0) -> B(dec_1) -> C(dec_2). A->B and B->C carry different reasons and dates.
A_TO_B = _link("dec_0", "licence change", "2026-03-10T00:00:00+00:00")
B_TO_C = _link("dec_2", "managed hosting", "2026-04-01T00:00:00+00:00")
D = {
    "decision_id": "dec_1",
    "content": "Use Postgres",
    "decision_type": "choice",
    "confidence": 0.7,
    "replaces": [A_TO_B],
    "replaced": B_TO_C,
}
# A was created in January and replaced in March; its own note describes A -> B.
OLD = _row("dec_0", "Use MySQL", created="2026-01-05T00:00:00+00:00",
           superseded_reason="licence change", superseded_at="2026-03-10T00:00:00+00:00")
# C's own note would describe C -> next; it must not be used for the B -> C line.
NEW = _row("dec_2", "Use Aurora", created="2026-02-20T00:00:00+00:00",
           superseded_reason="WRONG later change", superseded_at="2026-09-09T00:00:00+00:00")
REF_ROW = {"memory_id": "m1", "evidence_ref": "ev:abc", "kind": "test", "checked_at": "2026-01-12T00:00:00Z"}
MEM_ROW = {"memory_id": "m2", "memory": {"item_id": "m2", "content": "Latency report Q1", "created_at": "2026-01-13"}}


def _text(monkeypatch, as_of=None, decision=None, **payload):
    body = {"decision": decision or D, "evidence": [], "superseded": [], "superseded_by": [], **payload}
    backend = RemoteBackend.__new__(RemoteBackend)
    backend._request = lambda method, path, **kw: body
    _use(backend, monkeypatch)
    return _tools()["decision_provenance"]("dec_1", as_of=as_of)


def _line(text, ident):
    return next(x for x in text.splitlines() if x.startswith("- " + ident))


def test_n1_superseded_rows_listed(monkeypatch):
    t = _text(monkeypatch, superseded=[OLD])
    assert "Superseded: 1 decisions" in t
    line = _line(t, "dec_0")
    for part in ("Use MySQL", "licence change", "2026-03-10"):
        assert part in line


def test_n2_successor_rows_listed(monkeypatch):
    t = _text(monkeypatch, superseded_by=[NEW])
    assert "Superseded by: 1 decisions" in t
    line = _line(t, "dec_2")
    for part in ("Use Aurora", "managed hosting", "2026-04-01"):
        assert part in line


def test_n3_every_evidence_row_listed(monkeypatch):
    t = _text(monkeypatch, evidence=[REF_ROW, MEM_ROW])
    assert "Evidence: 2 items" in t
    assert "- ev:abc (test, checked 2026-01-12T00:00:00Z)" in t
    assert "Latency report Q1" in t and "2026-01-13" in t


def test_n4_no_history_has_no_headers(monkeypatch):
    # N13: a populated call must show its rows, then the empty call shows none.
    populated = _text(monkeypatch, superseded=[OLD], superseded_by=[NEW], evidence=[MEM_ROW])
    for header in ("Evidence: 1", "Superseded: 1", "Superseded by: 1"):
        assert header in populated
    t = _text(monkeypatch)
    for header in ("Evidence", "Superseded"):
        assert header not in t


def test_n5_missing_fields_print_no_none(monkeypatch):
    t = _text(
        monkeypatch,
        superseded=[{"decision_id": "dec_0"}],
        superseded_by=[{"decision_id": "dec_2", "content": "Use Aurora"}],
        evidence=[{"memory_id": "m3", "evidence_ref": "ev:x"}],
    )
    assert "None" not in t
    assert "dec_0" in t and "dec_2" in t and "ev:x" in t


def test_n6_long_content_trimmed_not_dropped(monkeypatch):
    long = "A" * 400
    t = _text(monkeypatch, superseded=[{**OLD, "content": long}])
    line = _line(t, "dec_0")
    assert "AAAA" in line and long not in line and "…" in line
    assert line.index("…") - line.index("A") <= 120


def test_n7_as_of_keeps_meaning(monkeypatch):
    t = _text(monkeypatch, superseded=[OLD], as_of="2026-04-15T00:00:00Z")
    assert "Superseded: 1 decisions" in t and "Superseded by" not in t
    assert "dec_0" in t


def test_n8_predecessor_line_has_change_reason_and_date_not_creation(monkeypatch):
    # From the replaces link
    line = _line(_text(monkeypatch, superseded=[OLD]), "dec_0")
    assert "reason: licence change" in line and "2026-03-10" in line
    assert "2026-01-05" not in line
    # Without a link: the predecessor's own context_snapshot note, still not created_at
    line = _line(_text(monkeypatch, superseded=[OLD], decision={**D, "replaces": []}), "dec_0")
    assert "reason: licence change" in line and "2026-03-10" in line and "2026-01-05" not in line


def test_n9_chain_each_line_has_its_own_transition(monkeypatch):
    t = _text(monkeypatch, superseded=[OLD], superseded_by=[NEW])
    before, after = _line(t, "dec_0"), _line(t, "dec_2")
    assert "licence change" in before and "2026-03-10" in before
    assert "managed hosting" in after and "2026-04-01" in after
    assert "WRONG" not in t and "2026-09-09" not in t


def test_n10_unusable_link_gives_no_reason_or_creation_date(monkeypatch):
    for bad in (_link("dec_2", "managed hosting", "2026-04-01", visible=False), _link("dec_other", "x", "2026-04-01")):
        line = _line(_text(monkeypatch, superseded_by=[NEW], decision={**D, "replaced": bad}), "dec_2")
        assert "reason" not in line and "2026-" not in line and "WRONG" not in line
        assert "dec_2" in line


def test_n11_dated_read_prints_no_neighbor_wording(monkeypatch):
    edited = {**NEW, "content": "Use Aurora, edited after the instant"}
    t = _text(monkeypatch, superseded=[OLD], superseded_by=[edited], as_of="2026-04-15T00:00:00Z")
    assert "edited after the instant" not in t and "Use MySQL" not in t
    assert "dec_0" in t and "licence change" in t and "managed hosting" in t
    # undated reads keep the wording
    assert "Use Aurora, edited after the instant" in _text(monkeypatch, superseded_by=[edited])


def test_n12_evidence_dates_from_partial_receipt_and_nested_creation(monkeypatch):
    partial = {"memory_id": "m4", "evidence_ref": None, "kind": None, "checked_at": "2026-02-02T00:00:00Z"}
    nested = {"memory_id": "m5", "memory": {"item_id": "m5", "content": "Nested one", "metadata": {"created_at": "2026-02-03"}}}
    flat = {"memory_id": "m6", "memory": {"item_id": "m6", "content": "Flat one", "metadata__created_at": "2026-02-04"}}
    t = _text(monkeypatch, evidence=[partial, nested, flat])
    assert "2026-02-02" in _line(t, "m4")
    assert "2026-02-03" in _line(t, "m5") and "Nested one" in _line(t, "m5")
    assert "2026-02-04" in _line(t, "m6")
