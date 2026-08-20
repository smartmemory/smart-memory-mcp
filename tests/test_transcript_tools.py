"""DIST-CC-INGEST-1 Phase 4 — MCP `transcript_search` / `transcript_status` tools.

These tools are the only ones that do NOT go through `resolve_backend()`: the ingested
transcript corpus lives in its own store, so they open a second SmartMemory against the
transcript dir. The tests below pin the behaviours that make that safe — a missing store
reports itself, the source filter maps to real origin prefixes, and an embedder
dimension mismatch is reported rather than returning zero hits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smartmemory_mcp.tools import transcript_tools as tt


def _registered() -> dict:
    captured: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def _decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorator

    tt.register(_FakeMCP())
    return captured


class _Item:
    def __init__(
        self,
        content,
        origin,
        title,
        item_id="i1",
        when="2026-08-13T07:10:51Z",
        cwd=None,
        git_branch=None,
        transcript_path=None,
    ):
        self.content = content
        self.origin = origin
        self.metadata = {"title": title}
        if cwd:
            self.metadata["cwd"] = cwd
        if git_branch:
            self.metadata["git_branch"] = git_branch
        if transcript_path:
            self.metadata["transcript_path"] = transcript_path
        self.item_id = item_id
        self.reference_time = when


class _Memory:
    """Stand-in for the transcript SmartMemory; records the search kwargs."""

    def __init__(self, results=None):
        self._results = results if results is not None else []
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self._results


@pytest.fixture(autouse=True)
def _reset():
    tt.reset_memory()
    yield
    tt.reset_memory()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A directory that looks like a real transcript store to `_store_exists`."""
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", str(tmp_path))
    (tmp_path / "memory.db").write_text("")
    # Dimensions agree unless a test says otherwise.
    monkeypatch.setattr(tt, "_index_dimension", lambda _d: 384)
    monkeypatch.setattr(tt, "_query_dimension", lambda: 384)
    return tmp_path


# -- data dir resolution ----------------------------------------------------------


def test_data_dir_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("SMARTMEMORY_TRANSCRIPTS_DIR", raising=False)
    assert tt.transcript_data_dir() == Path("~/.smartmemory-transcripts").expanduser()
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", "/tmp/elsewhere")
    assert tt.transcript_data_dir() == Path("/tmp/elsewhere")


def test_store_exists_requires_db_not_just_dir(tmp_path):
    # create_lite_memory() creates the directory on open, so a directory-only check
    # would report a store that holds nothing.
    assert not tt._store_exists(tmp_path)
    (tmp_path / "memory.db").write_text("")
    assert tt._store_exists(tmp_path)


# -- not-yet-imported -------------------------------------------------------------


def test_search_reports_missing_store_instead_of_empty_results(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", str(tmp_path / "nope"))
    out = _registered()["transcript_search"]("anything")
    assert "No transcript store" in out
    assert "transcripts index --detach" in out


def test_status_reports_missing_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", str(tmp_path / "nope"))
    assert "No transcript store" in _registered()["transcript_status"]()


# -- argument validation ----------------------------------------------------------


def test_empty_query_rejected(store):
    assert "`query` is required" in _registered()["transcript_search"]("   ")


def test_unknown_source_rejected(store):
    out = _registered()["transcript_search"]("q", source="bogus")
    assert "unknown source" in out


# -- source filter maps to real origin prefixes -----------------------------------


@pytest.mark.parametrize(
    "source,expected_origin",
    [
        ("all", "import:"),
        ("claude-code", "import:claude_code"),
        ("claude_code", "import:claude_code"),
        ("codex", "import:codex"),
    ],
)
def test_source_maps_to_origin_prefix(store, monkeypatch, source, expected_origin):
    mem = _Memory()
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    _registered()["transcript_search"]("q", source=source)
    assert mem.calls[0]["origin"] == expected_origin


def test_top_k_is_forwarded(store, monkeypatch):
    mem = _Memory()
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    _registered()["transcript_search"]("q", top_k=11)
    assert mem.calls[0]["top_k"] == 11


# -- rendering --------------------------------------------------------------------


def test_hits_render_source_title_and_id(store, monkeypatch):
    mem = _Memory(
        [_Item("we chose SQLite", "import:claude_code", "ruze: storage", "abc")]
    )
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("storage")
    assert "[claude_code]" in out
    assert "ruze: storage" in out
    assert "we chose SQLite" in out
    assert "abc" in out


def test_long_content_is_truncated(store, monkeypatch):
    mem = _Memory([_Item("x" * 2000, "import:codex", "t")])
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("q")
    assert "…" in out
    assert len(out) < 1200


def test_no_hits_points_at_status_not_silence(store, monkeypatch):
    monkeypatch.setattr(tt, "_get_memory", lambda: _Memory([]))
    out = _registered()["transcript_search"]("q")
    assert "No matching sessions" in out
    assert "transcript_status()" in out


# -- embedder mismatch is reported, never silently empty --------------------------


def test_mismatch_detected_when_dimensions_differ(store, monkeypatch):
    monkeypatch.setattr(tt, "_query_dimension", lambda: 1536)
    msg = tt._embedder_mismatch(store)
    assert msg is not None and "384" in msg and "1536" in msg


def test_mismatch_absent_when_dimensions_agree(store):
    assert tt._embedder_mismatch(store) is None


def test_mismatch_absent_when_undetectable(store, monkeypatch):
    # Unreadable metadata must not manufacture a false alarm.
    monkeypatch.setattr(tt, "_index_dimension", lambda _d: None)
    assert tt._embedder_mismatch(store) is None


def test_search_short_circuits_on_mismatch(store, monkeypatch):
    monkeypatch.setattr(tt, "_query_dimension", lambda: 1536)
    mem = _Memory([_Item("c", "import:codex", "t")])
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("q")
    assert "Embedding mismatch" in out
    assert mem.calls == []  # never ran a query that would return nothing


# -- status ------------------------------------------------------------------------


def _seed_ledger(store, entries):
    from smartmemory.importers.ledger import LEDGER_FILENAME, TranscriptLedger

    ledger = TranscriptLedger(store / LEDGER_FILENAME)
    for path, status, turns in entries:
        ledger.record(
            store / path, status=status, turns=turns, source_format="claude_code"
        )
    return ledger


def test_status_reports_ingested_turns_and_file_counts(store):
    _seed_ledger(store, [("a.jsonl", "ingested", 40), ("b.jsonl", "ingested", 122)])
    out = _registered()["transcript_status"]()
    assert "162" in out  # turns
    assert "ingested=2" in out
    assert "No import currently running." in out


def test_status_names_interrupted_sessions_as_duplication_risk(store):
    _seed_ledger(store, [("a.jsonl", "in_progress", 10)])
    out = _registered()["transcript_status"]()
    assert "Interrupted mid-ingest" in out
    assert "duplicate" in out


def test_status_surfaces_failures_with_retry_hint(store):
    _seed_ledger(store, [("a.jsonl", "failed", 0)])
    out = _registered()["transcript_status"]()
    assert "Failed files: 1" in out
    assert "--retry-failed" in out


def test_status_reports_mismatch_alongside_progress(store, monkeypatch):
    monkeypatch.setattr(tt, "_query_dimension", lambda: 1536)
    _seed_ledger(store, [("a.jsonl", "ingested", 5)])
    out = _registered()["transcript_status"]()
    assert "Embedding mismatch" in out
    assert "Turns ingested" in out


# -- provenance rendering + project filter (DIST-CC-INGEST-1 provenance) -----------


def test_hit_renders_provenance_when_present(store, monkeypatch):
    mem = _Memory(
        [
            _Item(
                "we chose SQLite",
                "import:claude_code",
                "widget: storage",
                cwd="/repo/widget",
                git_branch="main",
                transcript_path="/home/u/.claude/projects/x/s.jsonl",
            )
        ]
    )
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("storage")
    assert "/repo/widget" in out
    assert "branch main" in out
    assert "s.jsonl" in out


def test_hit_omits_provenance_line_when_absent(store, monkeypatch):
    # Pre-provenance imports must not render an empty "ran in:" line.
    mem = _Memory([_Item("c", "import:codex", "t")])
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("q")
    assert "ran in:" not in out
    assert "transcript:" not in out


def test_project_filter_keeps_only_matching_cwd(store, monkeypatch, tmp_path):
    root = tmp_path / "widget"
    root.mkdir()
    mem = _Memory(
        [
            _Item("a", "import:claude_code", "in", item_id="in", cwd=str(root)),
            _Item(
                "b", "import:claude_code", "out", item_id="out", cwd="/somewhere/else"
            ),
        ]
    )
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("q", project=str(root))
    assert "1 session(s)" in out
    assert "item_id: in" in out
    assert "item_id: out" not in out


def test_project_filter_matches_subdirectories(store, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    mem = _Memory(
        [_Item("a", "import:claude_code", "sub", cwd=str(root / "packages" / "api"))]
    )
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    assert "1 session(s)" in _registered()["transcript_search"]("q", project=str(root))


def test_project_filter_does_not_match_sibling_prefix(store, monkeypatch, tmp_path):
    # /repo must not match /repo-old — a plain startswith would.
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "repo-old").mkdir()
    mem = _Memory(
        [_Item("a", "import:claude_code", "sib", cwd=str(tmp_path / "repo-old"))]
    )
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    assert "No matching sessions" in _registered()["transcript_search"](
        "q", project=str(root)
    )


def test_project_filter_reports_items_without_provenance(store, monkeypatch, tmp_path):
    root = tmp_path / "widget"
    root.mkdir()
    mem = _Memory([_Item("a", "import:claude_code", "old")])  # no cwd
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    out = _registered()["transcript_search"]("q", project=str(root))
    assert "no recorded working directory" in out
    assert "re-import" in out


def test_project_filter_overfetches(store, monkeypatch, tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    mem = _Memory([])
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    _registered()["transcript_search"]("q", top_k=5, project=str(root))
    # Filtering happens after retrieval, so the query must ask for more than top_k.
    assert mem.calls[0]["top_k"] > 5


def test_overfetch_is_bounded(store, monkeypatch, tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    mem = _Memory([])
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    _registered()["transcript_search"]("q", top_k=1000, project=str(root))
    assert mem.calls[0]["top_k"] <= tt._PROJECT_OVERFETCH_MAX


def test_no_project_means_no_overfetch(store, monkeypatch):
    mem = _Memory([])
    monkeypatch.setattr(tt, "_get_memory", lambda: mem)
    _registered()["transcript_search"]("q", top_k=5)
    assert mem.calls[0]["top_k"] == 5
