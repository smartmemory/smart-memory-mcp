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
    assert "Latency report Q1" in _line(populated, "m2")
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
    # Dated shape: a dated read is served without blocks, so the selected decision has no replaces/replaced link.
    dated = {k: v for k, v in D.items() if k not in ("replaces", "replaced")}
    t = _text(monkeypatch, superseded=[OLD], decision=dated, as_of="2026-04-15T00:00:00Z")
    assert "Superseded: 1 decisions" in t and "Superseded by" not in t
    assert _line(t, "dec_0")


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


# ---------------------------------------------------------------------------------------------------------------
# Fix round 2: the census matrix (redesign eval round 2). Every cell of
#   read mode {undated, dated} x row kind {receipt, no-ref receipt, plain memory, predecessor, successor}
# is pinned on the row's OWN line. Core's two producer shapes:
#   undated  smart-memory-core queries.py `_provenance_selected` (:699-797): selected decision served with blocks,
#            so it carries `replaces`/`replaced` links; neighbors are the current rows.
#   dated    queries.py `_decision_provenance_as_of` (:799-823) + `_decision_who` without blocks
#            (smart_memory.py:5537): NO `replaced`/`replaces`; the selected decision carries `superseded_by` (a
#            successor id) with its folded note in context_snapshot (decisions/as_of.py:446-464, :522;
#            lifecycle_notes.py:568-575); neighbors are at_state copies of their CURRENT rows; evidence is filtered
#            on creation time only and keeps the current memory payload (queries.py:823).
# ---------------------------------------------------------------------------------------------------------------

import pytest

from smartmemory_mcp.tools import decision_tools

KIND_SECTION = {
    "receipt": "evidence",
    "noref_receipt": "evidence",
    "plain_memory": "evidence",
    "predecessor": "superseded",
    "successor": "superseded_by",
}
MODES = (False, True)  # dated?
AS_OF = "2026-04-15T00:00:00Z"
CELLS = [(kind, dated) for kind in KIND_SECTION for dated in MODES]

_RECEIPT = {
    "memory_id": "m1",
    "evidence_ref": "ev:abc",
    "kind": "test",
    "checked_at": "2026-01-12T00:00:00Z",
    "memory": {"item_id": "m1", "content": "RECEIPT WORDING", "created_at": "2026-01-01"},
}
_NOREF = {
    "memory_id": "m4",
    "evidence_ref": None,
    "kind": None,
    "checked_at": "2026-02-02T00:00:00Z",
    "memory": {"item_id": "m4", "content": "Checked wording", "created_at": "2026-01-13"},
}
_PLAIN = {"memory_id": "m2", "memory": {"item_id": "m2", "content": "Latency report Q1", "created_at": "2026-01-13"}}
# Dated shape of the selected decision B: no links; B -> C carried as superseded_by + the folded note.
_B_DATED_SUCCESSOR = {
    "decision_id": "dec_1",
    "content": "Use Postgres",
    "superseded_by": "dec_2",
    "context_snapshot": {"superseded_reason": "managed hosting", "superseded_at": "2026-04-01T00:00:00+00:00"},
}
_B_DATED_PLAIN = {k: v for k, v in D.items() if k not in ("replaces", "replaced")}
# R5: dated evidence reads use the block-free dated selected decision (core serves no blocks on a dated read).
_EVIDENCE_SELECTED = {False: D, True: _B_DATED_PLAIN}
_C_EDITED = {**NEW, "content": "Use Aurora, edited later"}
_A_EDITED = {**OLD, "content": "Use MySQL, edited later"}


def _cell_case(kind, dated):
    """(payload kwargs, selected decision, line id, must appear in the line, must not appear in the line)."""
    if kind == "receipt":
        must = ["test", "checked 2026-01-12"]
        return {"evidence": [_RECEIPT]}, _EVIDENCE_SELECTED[dated], "ev:abc", must, ["RECEIPT WORDING", "2026-01-01"]
    if kind == "noref_receipt":
        must = ["2026-01-13", "checked 2026-02-02"]
        wording = "Checked wording"
        return ({"evidence": [_NOREF]}, _EVIDENCE_SELECTED[dated], "m4", must if dated else must + [wording], [wording] if dated else [])
    if kind == "plain_memory":
        must = ["2026-01-13"] if dated else ["2026-01-13", "Latency report Q1"]
        return {"evidence": [_PLAIN]}, _EVIDENCE_SELECTED[dated], "m2", must, ["Latency report Q1"] if dated else []
    if kind == "predecessor":
        # Two dates per row: created 2026-01-05, replaced 2026-03-10.
        must = ["reason: licence change", "2026-03-10"] + ([] if dated else ["Use MySQL, edited later"])
        not_ = ["2026-01-05"] + (["Use MySQL"] if dated else [])
        return {"superseded": [_A_EDITED]}, (_B_DATED_PLAIN if dated else D), "dec_0", must, not_
    # successor: its own note (WRONG, 2026-09-09) and creation (2026-02-20) are never the B -> C transition.
    must = ["reason: managed hosting", "2026-04-01"] + ([] if dated else ["Use Aurora, edited later"])
    not_ = ["WRONG", "2026-09-09", "2026-02-20"] + (["Use Aurora"] if dated else [])
    return {"superseded_by": [_C_EDITED]}, (_B_DATED_SUCCESSOR if dated else D), "dec_2", must, not_


@pytest.mark.parametrize("kind,dated", CELLS, ids=[f"{k}-{'dated' if d else 'undated'}" for k, d in CELLS])
def test_n14_census_matrix_cell(monkeypatch, kind, dated):
    payload, decision, ident, must, must_not = _cell_case(kind, dated)
    t = _text(monkeypatch, as_of=AS_OF if dated else None, decision=decision, **payload)
    line = _line(t, ident)
    for part in must:
        assert part in line, (part, line)
    for part in must_not:
        assert part not in line, (part, line)
    assert "None" not in line


class _KeyRecorder(dict):
    """Records every section key the renderer asks for, whatever its header or count-unit spelling."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def get(self, key, default=None):
        self.asked.append(key)
        return default


def test_n15_census_guard_matrix_covers_every_section_and_mode():
    """A new section in `_provenance_rows` fails here until it gets cells (R6: keys recorded, not parsed)."""
    recorder = _KeyRecorder()
    decision_tools._provenance_rows(recorder)
    sections = set(recorder.asked) - {"decision"}
    assert sections == {"evidence", "superseded", "superseded_by"}
    assert set(KIND_SECTION.values()) == sections
    assert {dated for _, dated in CELLS} == {False, True}
    assert len(CELLS) == len(KIND_SECTION) * len(MODES) == 10


# ---------------------------------------------------------------------------------------------------------------
# BUG-MCP-TRACE-SUCCESSOR-SNAPSHOT-FALLBACK-1 (notes R1..R4). Core serves the `superseded_by` pointer and the
# `context_snapshot` note on an ordinary decision too; an undated line may use only a visible, matching link.
# ---------------------------------------------------------------------------------------------------------------

_B_WITH_POINTER = {
    **D,
    "superseded_by": "dec_2",
    "context_snapshot": {"superseded_reason": "SNAPSHOT reason", "superseded_at": "2026-05-05T00:00:00+00:00"},
}


@pytest.mark.parametrize(
    "bad",
    [_link("dec_2", "managed hosting", "2026-04-01", visible=False), _link("dec_other", "x", "2026-04-01"), None],
    ids=["invisible-link", "link-names-another-id", "no-link"],
)
def test_r1_r2_undated_successor_without_matching_visible_link_has_no_transition(monkeypatch, bad):
    line = _line(_text(monkeypatch, superseded_by=[NEW], decision={**_B_WITH_POINTER, "replaced": bad}), "dec_2")
    assert "dec_2" in line and "Use Aurora" in line
    for part in ("reason", "SNAPSHOT", "2026-05-05", "managed hosting", "2026-04-01", "WRONG"):
        assert part not in line, (part, line)


def test_r3_visible_link_wins_undated_and_dated_keeps_snapshot(monkeypatch):
    undated = _line(_text(monkeypatch, superseded_by=[NEW], decision={**_B_WITH_POINTER, "replaced": B_TO_C}), "dec_2")
    assert "reason: managed hosting" in undated and "2026-04-01" in undated and "SNAPSHOT" not in undated
    dated_b = {k: v for k, v in _B_WITH_POINTER.items() if k not in ("replaces", "replaced")}
    dated = _line(_text(monkeypatch, superseded_by=[NEW], decision=dated_b, as_of=AS_OF), "dec_2")
    assert "reason: SNAPSHOT reason" in dated and "2026-05-05" in dated


@pytest.mark.parametrize("dated", MODES, ids=["undated", "dated"])
def test_r4_receipt_with_check_time_and_no_kind(monkeypatch, dated):
    sparse = {"memory_id": "m7", "evidence_ref": "ev:sparse", "kind": None, "checked_at": "2026-01-12T00:00:00Z"}
    t = _text(monkeypatch, evidence=[sparse], decision=_EVIDENCE_SELECTED[dated], as_of=AS_OF if dated else None)
    assert _line(t, "ev:sparse") == "- ev:sparse (checked 2026-01-12T00:00:00Z)"
