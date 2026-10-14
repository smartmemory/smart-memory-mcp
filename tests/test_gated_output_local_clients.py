"""U5.21 local MCP clients consume core's gated output. # trace G:G44,G132

Bug class (C3/C8): a client reads "nothing / fewer rows" as "that is all there is". Core's read gate can stop a lane
at its scan budget, and says so on ``SearchResults.diagnostics.coverage``. ``transcript_search`` and the local
``list_memories`` must say the answer was partial instead of presenting it as definitive, and the OFF arm of
``sm2_search_visibility_gate`` must keep the legacy text and log exactly.

`LocalBackend.search`/`normalize_item` need no change: the ``read_status`` carrier is already validated by U5.22.
G149 (context graph) is N/A for these clients: neither tool accepts a gate or context argument.
"""

from __future__ import annotations

import logging

import pytest

from smartmemory.configuration.switch_testing import switch_override
from smartmemory.search.results import SearchDiagnostics, SearchResults
from smartmemory.search.visibility_gate import SWITCH

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import transcript_tools as tt

PARTIAL = {"read_gate_scan_budget_spent": True}
COMPLETE = {"complete": True}
UNREADABLE = {"complete": "no"}


class _Item:
    def __init__(self, item_id, cwd=None):
        self.item_id = item_id
        self.content = f"content of {item_id}"
        self.origin = "import:claude_code"
        self.metadata = {"title": f"title {item_id}", **({"cwd": cwd} if cwd else {})}
        self.reference_time = "2026-08-13T07:10:51Z"


class _Memory:
    def __init__(self, rows, coverage):
        self._rows = rows
        self._coverage = coverage
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(kwargs)
        return SearchResults(
            list(self._rows), SearchDiagnostics(coverage=self._coverage)
        )


def _search(monkeypatch, memory, **kwargs):
    tools = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    tt.register(_FakeMCP())
    monkeypatch.setattr(tt, "_get_memory", lambda: memory)
    return tools["transcript_search"]("storage", **kwargs)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    tt.reset_memory()
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / "memory.db").write_text("")
    monkeypatch.setattr(tt, "_index_dimension", lambda _d: 384)
    monkeypatch.setattr(tt, "_query_dimension", lambda: 384)
    yield tmp_path
    tt.reset_memory()


NOTE = (
    "\nThe search was partial (core reported: read_gate_scan_budget_spent), so matching sessions past what it "
    "examined may not be shown."
)
PARTIAL_WARNING = (
    "transcript search returned a partial answer under the read gate: core reported read_gate_scan_budget_spent, "
    "so visible matches past what it examined are not shown"
)

# (case, gate on, rows, coverage, text the answer must end with or contain, exact WARNING list). Every row is a
# short page: only the coverage tells a partial one from a complete one.
TRANSCRIPT_ROWS = [
    ("on partial with a hit", True, [_Item("a")], PARTIAL, NOTE, [PARTIAL_WARNING]),
    ("on partial with no hit", True, [], PARTIAL, NOTE, [PARTIAL_WARNING]),
    ("on complete with a hit", True, [_Item("a")], COMPLETE, None, []),
    ("on complete with no hit", True, [], COMPLETE, None, []),
    (
        "off partial with a hit is the legacy text",
        False,
        [_Item("a")],
        PARTIAL,
        None,
        [],
    ),
    ("off partial with no hit is the legacy text", False, [], PARTIAL, None, []),
]


@pytest.mark.parametrize(
    "case,on,rows,coverage,note,warnings",
    TRANSCRIPT_ROWS,
    ids=[r[0] for r in TRANSCRIPT_ROWS],
)
def test_transcript_search_says_when_core_answered_partially(
    monkeypatch, caplog, case, on, rows, coverage, note, warnings
):
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, on):
        legacy = _search(monkeypatch, _Memory(rows, COMPLETE))
        caplog.clear()
        out = _search(monkeypatch, _Memory(rows, coverage))
    if note:
        assert out.replace(NOTE, "") == legacy and NOTE in out
    else:
        assert out == legacy and "The search was partial" not in out
    assert [r.getMessage() for r in caplog.records] == warnings


def test_transcript_partial_note_survives_the_project_filter(monkeypatch):
    """The over-fetch + project filter drops rows: the partial note must still be said with the filter's own note."""
    rows = [_Item("a", cwd="/elsewhere"), _Item("b", cwd="/repo/sub")]
    with switch_override(SWITCH, True):
        out = _search(monkeypatch, _Memory(rows, PARTIAL), project="/repo")
    assert NOTE in out
    assert "title b" in out and "title a" not in out


def test_transcript_unreadable_coverage_is_a_refusal_not_a_complete_answer(
    monkeypatch, caplog
):
    """C8: a coverage core could not have written is never read as complete. The typed refusal reaches the tool."""
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True), pytest.raises(ValueError) as refused:
        _search(monkeypatch, _Memory([_Item("a")], UNREADABLE))
    assert type(refused.value).__name__ == "PartialCoverageUnreadable"
    assert [r.getMessage() for r in caplog.records] == [
        "Search coverage refused: coverage['complete'] is a str, not a bool, so the result cannot be read as "
        "complete or partial"
    ]


# -- local list_memories ---------------------------------------------------------------------------------------------


def _local(rows, coverage) -> tuple[LocalBackend, _Memory]:
    memory = _Memory(rows, coverage)
    backend = object.__new__(LocalBackend)
    backend._mem = memory
    return backend, memory


def _row(n):
    return {
        "item_id": f"r{n}",
        "content": f"c{n}",
        "memory_type": "semantic",
        "metadata": {},
    }


LIST_ROWS = [
    (
        "on partial warns once",
        True,
        PARTIAL,
        [
            "list_memories returned a partial answer under the read gate: core reported read_gate_scan_budget_spent, "
            "so visible matches past what it examined are not shown"
        ],
    ),
    ("on complete is silent", True, COMPLETE, []),
    ("off partial is silent", False, PARTIAL, []),
]


@pytest.mark.parametrize(
    "case,on,coverage,warnings", LIST_ROWS, ids=[r[0] for r in LIST_ROWS]
)
def test_list_memories_names_a_partial_answer_and_returns_the_same_rows(
    caplog, case, on, coverage, warnings
):
    caplog.set_level(logging.WARNING)
    backend, memory = _local([_row(n) for n in range(3)], coverage)
    with switch_override(SWITCH, on):
        got = backend.list_memories(limit=10)
    assert [r["item_id"] for r in got] == ["r0", "r1", "r2"]
    assert [r.getMessage() for r in caplog.records] == warnings
    assert memory.calls == [{"top_k": 200}]


def test_local_search_hands_the_core_read_status_carrier_through(monkeypatch, caplog):
    """The status/cursor class: the normalizer must not drop the carrier core put on the row (validated by U5.22)."""
    from smartmemory_app import storage

    caplog.set_level(logging.WARNING)
    carrier = {"extraction_state": "extraction_pending"}
    memory = _Memory([{**_row(0), "read_status": carrier}], COMPLETE)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: memory)
    backend = object.__new__(LocalBackend)
    backend._mem = memory
    with switch_override(SWITCH, True):
        got = backend.search("atlas", top_k=5)
    assert got[0]["read_status"] == carrier
    assert caplog.records == []
