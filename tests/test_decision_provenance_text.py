"""BUG-PROVENANCE-COUNTS-ONLY-1: the provenance tool text lists each row, not only counts.

One test per frozen note (units/BUG-PROVENANCE-COUNTS-ONLY-1/notes-frozen.md), N1..N7.
"""

from __future__ import annotations

from smartmemory_mcp.backends.remote import RemoteBackend

from tests.test_decision_tools_as_of import _tools, _use

D = {"decision_id": "dec_1", "content": "Use Postgres", "decision_type": "choice", "confidence": 0.7}
OLD = {
    "decision_id": "dec_0",
    "content": "Use MySQL",
    "superseded_reason": "licence change",
    "superseded_at": "2026-01-10T00:00:00+00:00",
}
NEW = {
    "decision_id": "dec_2",
    "content": "Use Aurora",
    "superseded_reason": "managed hosting",
    "superseded_at": "2026-02-01T00:00:00+00:00",
}
REF_ROW = {"memory_id": "m1", "evidence_ref": "ev:abc", "kind": "test", "checked_at": "2026-01-12T00:00:00Z"}
MEM_ROW = {"memory_id": "m2", "memory": {"item_id": "m2", "content": "Latency report Q1", "created_at": "2026-01-13"}}


def _text(monkeypatch, as_of=None, **payload):
    body = {"decision": D, "evidence": [], "superseded": [], "superseded_by": [], **payload}
    backend = RemoteBackend.__new__(RemoteBackend)
    backend._request = lambda method, path, **kw: body
    _use(backend, monkeypatch)
    return _tools()["decision_provenance"]("dec_1", as_of=as_of)


def test_n1_superseded_rows_listed(monkeypatch):
    t = _text(monkeypatch, superseded=[OLD])
    assert "Superseded: 1 decisions" in t
    for part in ("dec_0", "Use MySQL", "licence change", "2026-01-10"):
        assert part in t


def test_n2_successor_rows_listed(monkeypatch):
    t = _text(monkeypatch, superseded_by=[NEW])
    assert "Superseded by: 1 decisions" in t
    for part in ("dec_2", "Use Aurora", "managed hosting", "2026-02-01"):
        assert part in t


def test_n3_every_evidence_row_listed(monkeypatch):
    t = _text(monkeypatch, evidence=[REF_ROW, MEM_ROW])
    assert "Evidence: 2 items" in t
    assert "- ev:abc (test, checked 2026-01-12T00:00:00Z)" in t
    assert "Latency report Q1" in t and "2026-01-13" in t


def test_n4_no_history_has_no_headers(monkeypatch):
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
    line = next(x for x in t.splitlines() if "dec_0" in x)
    assert "AAAA" in line and long not in line and "…" in line


def test_n7_as_of_keeps_meaning(monkeypatch):
    t = _text(monkeypatch, superseded=[OLD], as_of="2026-01-15T00:00:00Z")
    assert "Superseded: 1 decisions" in t and "Superseded by" not in t
    assert "dec_0" in t
