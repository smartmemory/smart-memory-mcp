"""U5.21 local MCP clients consume core's gated output through the REAL core. # trace G:G44,G132,G149

Bug class (C3/C8, normalizer drops completeness): a client reads "nothing / fewer rows" as "that is all there is".
Core's read gate can stop a lane at its scan budget and says so on ``SearchResults.diagnostics.coverage``. Every local
client that turns a search into model-facing text must say the answer was partial instead of a definitive "No X found".

Nothing here fabricates core: each case builds a real lite SQLite store, binds the real pending read port and the real
``LaneReadGate`` (only its ``max_scan`` is lowered, the one knob a six-row store needs), and calls the real MCP tool
through the real ``LocalBackend`` and the real wrapper ``storage.search``. The switch OFF arm is paired with every ON arm.

Ownership of the G rows that this unit does NOT exercise here (named, not blanket-passed):
  * G132 (admin-scoped, content-redacted paused-work decision view): decision at FULL-ALIGN-1/design.md:409, owned by
    SM2-IDENTITY-1 (its design.md:40). These clients expose no paused-work view and no admin scope argument.
  * G149 (context graph identity, context structure excluded from ordinary recall): decision at
    FULL-ALIGN-1/design.md:462, owned by SM2-CONTEXT-1 (its design.md:21). These clients pass no context anchor,
    membership or structure argument (``test_the_wrapper_drops_read_gate_and_context_kwargs`` in the wrapper suite).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from smartmemory.configuration.switch_testing import switch_override
from smartmemory.save.read_port import InMemoryPendingStore
from smartmemory.search import facade_readers
from smartmemory.search.lane_gate import LaneReadGate
from smartmemory.search.visibility_gate import SWITCH
from smartmemory.tools.factory import create_lite_memory

from smartmemory_app import storage
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import (
    code_tools,
    dev_tools,
    evolution_tools,
    memory_tools,
    reasoning_tools,
    transcript_tools,
)

LANE_LOG = "smartmemory.search.lane_gate"
GATE_LOG = "smartmemory.search.visibility_gate"
OFF_SWITCH_WARNING = (
    GATE_LOG,
    "WARNING",
    "switch sm2_search_visibility_gate OFF: search keeps only the legacy post-retrieval visibility filter; "
    "no unified read gate is bound",
)
MEMORY_TOOLS_LOG = "smartmemory_mcp.tools.memory_tools"
LOCAL_LOG = "smartmemory_mcp.backends.local"
STORAGE_LOG = "smartmemory_app.storage"
RESULTS_LOG = "smartmemory.search.results"
# Every log assertion is the ENTIRE ordered (logger, level, message) list across every logger (fix round 2, F4).
# Refusal rows and rows whose callee is a stand-in assert the complete list (``full_log``). Rows on the real store assert
# the complete list minus NOISE: a closed list of (logger, message prefix) pairs the real facade logs for its environment,
# never for this unit, measured by ``u-U5.21/fix2/noise_probe.py`` (31-191 records per call, some non-deterministic: the
# reranker's background load, a once-per-process Redis notice, the read-runner OFF notice). Core's own lane budget line
# is listed because core's ``test_search_gate_partial_coverage`` pins its exact text. Anything else fails the row.
NOISE = (
    ("smartmemory.search.rerank", "Reranker "),
    ("smartmemory.search.rerank", "Reranking took "),
    ("smartmemory.redis_gate", "activation boost disabled"),
    ("smartmemory.redis_gate", "retrieval tracking disabled"),
    ("smartmemory.search.read_runner_adapter", "switch sm2_read_runner_default OFF"),
    (
        "smartmemory.memory.pipeline.stages.search",
        "SmartGraph returned no usable candidates",
    ),
    (
        "smartmemory.memory.pipeline.stages.search",
        "Manual similarity scan rejected item ",
    ),
    ("smartmemory.smart_memory", "Strength ranking SKIPPED"),
    ("smartmemory.smart_memory", "Search refill cap"),
    ("smartmemory.models.rerank", "rerank_unscored"),
    ("smartmemory.search.pending_facade", "Extraction status unavailable for"),
    (LANE_LOG, "Read gate scan budget spent in lane "),
    ("smartmemory.search.visibility_pages", "SMARTMEMORY_PAGE_CURSOR_KEY is unset"),
)
LIST_TOTAL_LOST = (
    MEMORY_TOOLS_LOG,
    "WARNING",
    "Memory list total is unavailable; corpus size was lost because the backend returned only a page list.",
)

# metadata each consumer needs on a row of its memory type
TYPE_META = {
    "semantic": {},
    "decision": {"title": "Atlas decision", "rationale": "because"},
    "episodic": {"summary": "atlas session"},
    "code": {
        "name": "atlas_fn",
        "entity_type": "function",
        "file_path": "atlas.py",
        "line_number": 1,
        "content_hash": "test_hash",
        "call_evidence": "test",
    },
    "reasoning": {"steps": ["a"]},
    "pending": {},
}
PARTIAL_PHRASE = "The answer was partial ("
NO_VISIBLE = "not proof that none exist"


class World:
    def __init__(self, mem, backend, tools):
        self.mem, self.backend, self.tools = mem, backend, tools


@pytest.fixture
def world(tmp_path, monkeypatch):
    mem = create_lite_memory(data_dir=str(tmp_path), spawn_worker=False)
    with switch_override(SWITCH, True):
        mem.bind_pending_reads(InMemoryPendingStore(), workspace_hidden=False)
    real = facade_readers.FacadeReadGate.lane_gate
    budget = {"max_scan": None}

    def lane_gate(self):
        bound = real(self)
        if budget["max_scan"] is None:
            return bound
        return LaneReadGate(bound.gate, bound.fetch, max_scan=budget["max_scan"])

    monkeypatch.setattr(facade_readers.FacadeReadGate, "lane_gate", lane_gate)
    monkeypatch.setattr(storage, "get_memory", lambda *a, **k: mem)
    backend = object.__new__(LocalBackend)
    backend._mem = mem
    for module in (
        memory_tools,
        dev_tools,
        code_tools,
        reasoning_tools,
        evolution_tools,
    ):
        monkeypatch.setattr(module, "get_backend", lambda: backend)
    tools = {}

    class Registration:
        def tool(self, *a, **kw):
            def decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return decorate

    memory_tools.register_free(Registration())
    memory_tools.register_pro(Registration())
    for module in (dev_tools, code_tools, reasoning_tools, evolution_tools):
        module.register(Registration())
    w = World(mem, backend, tools)
    w.set_budget = lambda n: budget.__setitem__("max_scan", n)
    try:
        yield w
    finally:
        mem.close()


def seed(mem, memory_type, visible, count=6, origin="user:test", extra=None):
    """``count`` rows of one memory type, matching "atlas budget". Only the indexes in ``visible`` are not retired."""
    for n in range(count):
        mem._graph.backend.add_node(
            f"test_u521_{memory_type}_{n}",
            {
                "content": f"atlas budget note {n}",
                "origin": origin,
                "retired": n not in visible,
                "created_at": "2026-01-01T00:00:00+00:00",
                **TYPE_META[memory_type],
                "metadata": dict(TYPE_META[memory_type]),
                **(extra or {}),
            },
            memory_type=memory_type,
        )


def shown(text):
    """The seeded ``semantic`` indexes a tool's text shows. The store ranks equal-scoring rows in no fixed order, so the
    rows assert WHICH CLASS of row is shown (hidden or visible) and how many, never one particular index."""
    return {int(n) for n in re.findall(r"test_u521_semantic_(\d)", text)}


def unknown_origin(count):
    """``memory_search`` runs ``filter_by_tiers`` on result dicts, which have no ``origin`` attribute, so every row it
    shows is counted as legacy 'unknown' (memory_tools.py:622, unchanged here): one line per call with rows."""
    return (
        "smartmemory.origin_policy",
        "WARNING",
        f"filter_by_tiers: {count} item(s) with origin='unknown' treated as always-visible (legacy-data fail-open, "
        "CORE-ORIGIN-1). If these were written by a NEW code path, it forgot to set a real origin — see "
        "origin_policy.py module docstring.",
    )


def full_log(caplog):
    """The ENTIRE WARNING-or-worse list, every logger, in order."""
    return [(r.name, r.levelname, r.getMessage()) for r in caplog.records]


def answer_log(caplog):
    """The entire list minus the closed ``NOISE`` list: for rows that run the real facade."""
    return [
        t
        for t in full_log(caplog)
        if not any(t[0] == name and t[2].startswith(prefix) for name, prefix in NOISE)
    ]


def gate_warning(where, names="complete, read_gate_scan_budget_spent"):
    return (
        GATE_LOG,
        "WARNING",
        f"{where} returned a partial answer under the read gate: core reported {names}, so visible matches past what "
        "it examined are not shown",
    )


def over(where, count, fetch):
    """Core's one fetch-bound WARNING (``visibility_gate.gated_fetch_bound``), the same text at every client site."""
    return (
        GATE_LOG,
        "WARNING",
        f"{where} returned {count} rows for a fetch of {fetch}: the rows past the fetch were never asked for and are "
        "not used",
    )


def call(w, tool, on, **kw):
    with switch_override(SWITCH, on):
        return w.tools[tool](**kw)


# -- the F1 reproduction: the real core says partial, the real MCP tool must not say "No results found" --------------


def test_f1_partial_empty_search_is_not_no_results_found(world, caplog):
    """Six semantic rows, the first five retired, scan budget 1: core returns 0 rows and says the answer is partial."""
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    on = call(world, "memory_search", True, query="atlas budget", top_k=3)
    assert "No results found" not in on
    assert on.startswith(
        "No visible results in the part core examined. " + PARTIAL_PHRASE
    )
    assert "read_gate_scan_budget_spent" in on and on.endswith(NO_VISIBLE + ".")
    assert answer_log(caplog) == [gate_warning("local search")]


def test_f1_all_visible_control_says_nothing_partial(world, caplog):
    """The control: every row visible, the same budget. Core still reports partial (more candidates unexamined), but
    the visible rows are shown with the partial note, and with a roomy budget there is no note at all."""
    seed(world.mem, "semantic", visible={0, 1, 2, 3, 4, 5})
    caplog.set_level(logging.WARNING)
    roomy = call(world, "memory_search", True, query="atlas budget", top_k=3)
    assert PARTIAL_PHRASE not in roomy and len(shown(roomy)) == 3
    assert answer_log(caplog) == [unknown_origin(3)]
    world.set_budget(1)
    caplog.clear()
    tight = call(world, "memory_search", True, query="atlas budget", top_k=3)
    assert (
        len(shown(tight)) == 1 and PARTIAL_PHRASE in tight and "No visible" not in tight
    )
    assert answer_log(caplog) == [gate_warning("local search"), unknown_origin(1)]


def test_f1_off_arm_is_the_legacy_answer(world, caplog):
    """OFF: no gate, no coverage read, no note, no client WARNING: rows the gate would hide are the legacy rows."""
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    off = call(world, "memory_search", False, query="atlas budget", top_k=3)
    # three rows of six, one visible (5): at least two are rows the gate hides ON
    assert PARTIAL_PHRASE not in off and "No visible" not in off
    assert len(shown(off)) == 3 and len(shown(off) - {5}) >= 2
    assert answer_log(caplog) == [OFF_SWITCH_WARNING, unknown_origin(3)]


# -- every other consumer of a gated search: one row per consumer (class: normalizer/consumer drops completeness) -----

# (case, tool, kwargs, memory_type seeded, legacy text prefix OFF, noun in the ON refusal, gated searches per call:
# dev_load_context searches sessions and then decisions, dev_tools.py:250 and :276, so it logs the partial answer twice)
CONSUMER_ROWS = [
    (
        "dev_query_decisions",
        "dev_query_decisions",
        {"query": "atlas budget"},
        "decision",
        "Found ",
        "decisions",
        1,
    ),
    (
        "dev_load_context sessions",
        "dev_load_context",
        {"topic": "atlas budget"},
        "episodic",
        "=== Recent",
        "recent sessions",
        2,
    ),
    (
        "code_search",
        "code_search",
        {"query": "atlas budget"},
        "code",
        "Found ",
        "code entities",
        1,
    ),
    (
        "reasoning_query_traces",
        "reasoning_query_traces",
        {"query": "atlas budget"},
        "reasoning",
        "Found ",
        "reasoning traces",
        1,
    ),
]


@pytest.mark.parametrize(
    "case,tool,kw,mtype,off_prefix,noun,searches",
    CONSUMER_ROWS,
    ids=[r[0] for r in CONSUMER_ROWS],
)
def test_every_consumer_of_a_partial_empty_search_says_partial_not_none(
    world, caplog, case, tool, kw, mtype, off_prefix, noun, searches
):
    seed(world.mem, mtype, visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    on = call(world, tool, True, **kw)
    assert f"No visible {noun} in the part core examined." in on
    assert PARTIAL_PHRASE in on and NO_VISIBLE in on
    assert answer_log(caplog) == [gate_warning("local search")] * searches
    caplog.clear()
    off = call(world, tool, False, **kw)
    assert off.startswith(off_prefix) and PARTIAL_PHRASE not in off
    assert answer_log(caplog) == [OFF_SWITCH_WARNING]


def test_evolution_status_with_a_partial_empty_search_is_not_idle(world):
    """``backend.search('*', memory_type='pending')`` resolves its own partial answer inside the wrapper (widen, else a
    typed refusal), so evolution_status sees a complete answer here: five retired + one visible pending row = 1."""
    seed(world.mem, "pending", visible={5})
    world.set_budget(1)
    on = call(world, "evolution_status", True)
    assert on == "Evolution status: ready\nPending memory items (formerly 'working'): 1"
    off = call(world, "evolution_status", False)
    assert (
        off == "Evolution status: ready\nPending memory items (formerly 'working'): 6"
    )


def test_memory_recall_with_a_partial_empty_answer_is_not_an_empty_string(
    world, caplog
):
    """The wrapper's recall returns text: a str carrying ``.partial``. An empty one used to reach the model as ''."""
    seed(world.mem, "semantic", visible=set())
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    on = call(world, "memory_recall", True, query="atlas budget", top_k=3)
    assert on.startswith(
        "No visible memories in the part core examined. " + PARTIAL_PHRASE
    )
    off = call(world, "memory_recall", False, query="atlas budget", top_k=3)
    assert PARTIAL_PHRASE not in off


def test_memory_recall_with_a_partial_nonempty_answer_carries_the_note(world):
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    on = call(world, "memory_recall", True, query="atlas budget", top_k=3)
    assert "test_u521_semantic_5" in on and PARTIAL_PHRASE in on


# -- memory_recall: a stop at the fetch bound reaches the model as partial (review r2 F1, through the real MCP tool) ----


def seed_capped(mem, eligible):
    """Review r2 F1: 201 visible rows newest first, the newest 200 below the recall confidence floor except the indexes
    in ``eligible``; the oldest row (index 200) is eligible but past the 200-row bound."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for n in range(201):
        mem._graph.backend.add_node(
            f"test_cap_{n:03d}",
            {
                "content": f"atlas budget {n}",
                "origin": "user:test",
                "created_at": (base + timedelta(seconds=200 - n)).isoformat(),
                "confidence": 1.0 if n == 200 or n in eligible else 0.01,
            },
            memory_type="semantic",
        )


# (case, newest rows made eligible, gate on, the answer's exact text or the ids it must name, partial phrase expected)
RECALL_CAP_ROWS = [
    (
        "on empty is partial, not ''",
        (),
        True,
        "No visible memories in the part core examined. The answer was partial (fetch_bound), so matches past what "
        "was examined may not be shown. This is not proof that none exist.",
        True,
    ),
    ("on non-empty carries the note", (0,), True, ["test_cap_000"], True),
    ("off empty is the legacy answer", (), False, ["test_cap_200"], False),
    (
        "off non-empty is the legacy answer",
        (0,),
        False,
        ["test_cap_000", "test_cap_200"],
        False,
    ),
]


@pytest.mark.parametrize(
    "case,eligible,on,answer,note", RECALL_CAP_ROWS, ids=[r[0] for r in RECALL_CAP_ROWS]
)
def test_memory_recall_cap_stop_reaches_the_model_as_partial(
    world, monkeypatch, case, eligible, on, answer, note
):
    from smartmemory_app import recall_format

    monkeypatch.setattr(recall_format, "_trace", lambda **kw: None)
    seed_capped(world.mem, eligible)
    out = call(world, "memory_recall", on, query="", top_k=3)
    if isinstance(answer, str):
        assert out == answer
    else:
        assert [
            i for i in (f"test_cap_{n:03d}" for n in range(201)) if f"[mem:{i}]" in out
        ] == answer
    assert ("The answer was partial (fetch_bound)" in out) is note


# -- memory_list: the paginated read (R11) --------------------------------------------------------------------------

# (case, rows visible, limit, offset, ids shown in order)
# Gate ON, pages are cut from core's frozen walk (LOCAL-FROZEN-PAGE-1): ``limit`` and ``offset`` bound the page, there is
# no cursor token for a client, and the page carries the walk's total. The walk reads every visible row, so a small lane
# scan budget no longer makes the list partial (see ``test_memory_list_is_exact_under_a_small_scan_budget``).
LIST_ROWS = [
    (
        "complete first page is bounded by limit",
        {0, 1, 2, 3, 4, 5},
        4,
        0,
        [0, 1, 2, 3],
    ),
    ("complete second page by offset", {0, 1, 2, 3, 4, 5}, 4, 4, [4, 5]),
    (
        "complete page past the end is empty and silent",
        {0, 1, 2, 3, 4, 5},
        4,
        9,
        [],
    ),
]


@pytest.mark.parametrize(
    "case,visible,limit,offset,shown",
    LIST_ROWS,
    ids=[r[0] for r in LIST_ROWS],
)
def test_memory_list_pages_under_both_coverage_states(
    world, caplog, case, visible, limit, offset, shown
):
    seed(world.mem, "semantic", visible=visible)
    caplog.set_level(logging.WARNING)
    out = call(world, "memory_list", True, limit=limit, offset=offset)
    assert out.count("- [test_u521_") == len(shown)
    assert [n for n in range(6) if f"test_u521_semantic_{n}]" in out] == shown
    assert PARTIAL_PHRASE not in out
    if not shown:
        assert "No memories returned for this page (offset 9); 6 memories exist." in out
    else:
        assert f"of 6 memories" in out
    assert answer_log(caplog) == []


def test_memory_list_is_exact_under_a_small_scan_budget(world, caplog):
    """The lane scan budget bounded the old wildcard read, so it made the page partial. The frozen walk reads every
    visible row on its own bound, so the same store lists both visible rows, names no partial and carries the total."""
    seed(world.mem, "semantic", visible={5})
    seed(world.mem, "episodic", visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    out = call(world, "memory_list", True, limit=4, offset=0)
    assert out.count("- [test_u521_") == 2 and "Showing 2 of 2 memories" in out
    assert PARTIAL_PHRASE not in out and answer_log(caplog) == []


def test_memory_list_off_arm_is_the_legacy_page(world, caplog):
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    out = call(world, "memory_list", False, limit=4, offset=0)
    # four rows of six, one visible (5): at least three are rows the gate hides ON
    assert PARTIAL_PHRASE not in out
    assert len(shown(out)) == 4 and len(shown(out) - {5}) >= 3
    assert answer_log(caplog) == [OFF_SWITCH_WARNING, LIST_TOTAL_LOST]  # OFF is the base's page and lines


# -- memory_list refusals: logged once by the origin when the gate is ON, the base's lines when it is OFF (F3, F4) -------

ORIGIN_LOG = "test_origin"
ORIGIN_LINE = (ORIGIN_LOG, "WARNING", "origin: page unavailable")


def tool_lost(text="page unavailable"):
    return (
        MEMORY_TOOLS_LOG,
        "WARNING",
        f"Memory list unavailable; page items and corpus total were lost because the backend list call failed: {text}",
    )


def _origin_marked():
    """A backend whose raising site logged its refusal and marked it (``smartmemory.refusal_logging``)."""
    from smartmemory.refusal_logging import mark_refusal_logged

    logging.getLogger(ORIGIN_LOG).warning("origin: page unavailable")
    raise mark_refusal_logged(RuntimeError("page unavailable"))


def _unmarked():
    raise RuntimeError("page unavailable")


class _OddText(str):
    pass


class OddTextError(RuntimeError):
    """``__str__`` answers a ``str`` subclass: ``%s`` renders it, ``refusal_text`` refuses to (not an exact ``str``), so
    the ON and OFF lines differ. ``tools/common.py:42`` lower-cases ``str(exc)``, which a subclass survives."""

    def __str__(self):
        return _OddText("odd page text")


def _odd_text():
    raise OddTextError()


def _core_unreadable(world, monkeypatch):
    """The real core path: the frozen walk's own refusal (``direct_lists._refuse``: one WARNING, a typed error)."""
    from smartmemory.search import direct_lists

    def refuse(**kwargs):
        raise direct_lists._refuse("the root read failed (test); the read was not completed")

    monkeypatch.setattr(world.mem, "read_gated_roots_page", refuse)


CORE_UNREADABLE = (
    "smartmemory.search.direct_lists",
    "WARNING",
    "Direct list read unavailable: the root read failed (test); the read was not completed",
)

# (case, fault, gate on, the error the tool raises (None: it answers), the mark on it afterwards, the COMPLETE list).
# Base (915bbdb) handler, the OFF contract: log "...backend list call failed: %s" with the exception, re-raise, no mark.
# ON: a refusal its origin already logged is not logged again, any other failure is logged here (bounded text) and
# marked so no caller logs it twice. OFF: the base's line for every failure, marked or not, and the tool marks nothing.
LIST_REFUSAL_ROWS = [
    (
        "on marked by its origin is logged once",
        _origin_marked,
        True,
        RuntimeError,
        True,
        [ORIGIN_LINE],
    ),
    (
        "on unmarked is logged by the tool and marked",
        _unmarked,
        True,
        RuntimeError,
        True,
        [tool_lost()],
    ),
    (
        "off marked keeps the base's two lines",
        _origin_marked,
        False,
        RuntimeError,
        True,
        [ORIGIN_LINE, tool_lost()],
    ),
    (
        "off unmarked is the base's line and no mark",
        _unmarked,
        False,
        RuntimeError,
        False,
        [tool_lost()],
    ),
    (
        "on odd text is rendered bounded",
        _odd_text,
        True,
        OddTextError,
        True,
        [tool_lost("<OddTextError could not be rendered>")],
    ),
    (
        "off odd text is the base's rendering",
        _odd_text,
        False,
        OddTextError,
        False,
        [tool_lost("odd page text")],
    ),
    (
        "on core refusal is logged by core only",
        "core",
        True,
        "VisibilityUnavailable",
        True,
        [CORE_UNREADABLE],
    ),
    (
        "off core does not read coverage",
        "core",
        False,
        None,
        None,
        [LIST_TOTAL_LOST],
    ),
]


@pytest.mark.parametrize(
    "case,fault,on,error,marked,warnings",
    LIST_REFUSAL_ROWS,
    ids=[r[0] for r in LIST_REFUSAL_ROWS],
)
def test_memory_list_logs_a_refusal_once_and_off_is_the_base(
    world, monkeypatch, caplog, case, fault, on, error, marked, warnings
):
    from smartmemory.refusal_logging import refusal_logged
    from smartmemory import visibility as results_module

    seed(world.mem, "semantic", visible={5})
    if fault == "core":
        _core_unreadable(world, monkeypatch)
    else:
        monkeypatch.setattr(world.backend, "list_memories", lambda **kw: fault())
    if isinstance(error, str):
        error = getattr(results_module, error)
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, on):
        if error is None:
            world.tools["memory_list"](limit=4, offset=0)
        else:
            with pytest.raises(error) as raised:
                world.tools["memory_list"](limit=4, offset=0)
            assert refusal_logged(raised.value) is marked
    assert full_log(caplog) == warnings


# -- memory_list: the window is a fetch bound, and a stop at it is carried (F1, F2 classes) ---------------------------

LIST_CAP_LINE = (
    LOCAL_LOG,
    "WARNING",
    "list_memories scanned the local cap (200 items); results beyond that window are not represented in this page or "
    "its offset.",
)


def seed_many(mem, count):
    for n in range(count):
        mem._graph.backend.add_node(
            f"test_u521_many_{n:03d}",
            {
                "content": f"atlas many {n}",
                "origin": "user:test",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            memory_type="semantic",
        )


class ListOverReturn:
    """The one stand-in for list's over-return rows (real core cannot be made to break its own bound): ``top_k + 1``
    dictionary rows, and only the LAST, past the window, has ``marker=yes``."""

    def __init__(self):
        self.calls = []

    def search(self, text, *, top_k, **kwargs):
        from smartmemory.search.results import SearchDiagnostics, SearchResults

        self.calls.append(top_k)
        rows = [
            {
                "item_id": f"test_over_{n}",
                "content": "atlas",
                "memory_type": "semantic",
                "metadata": {"marker": "yes" if n == top_k else "no"},
            }
            for n in range(top_k + 1)
        ]
        return SearchResults(rows, SearchDiagnostics(coverage={"complete": True}))


# (case, gate on, how the store answers, ids on the page, names carried, the WARNING list). The real-store rows hold 201
# visible rows. ON: the frozen walk has no 200-row window, so all 201 are paged and nothing is partial; OFF: the legacy
# wildcard window comes back full and the page carries only the legacy line. The stand-in rows (OFF only: the stand-in
# has no walk) over-return one row past the window that alone matches the filter, and the legacy list pages it.
LIST_BOUND_ROWS = [
    (
        "on full store pages in full, no window and no fetch_bound",
        True,
        "real",
        4,
        (),
        [],
    ),
    (
        "off full window is the legacy page",
        False,
        "real",
        4,
        (),
        [OFF_SWITCH_WARNING, LIST_CAP_LINE],
    ),
    (
        "off over-return is the legacy page",
        False,
        "over",
        ["test_over_200"],
        (),
        [LIST_CAP_LINE],
    ),
]


@pytest.mark.parametrize(
    "case,on,store,page,names,warnings",
    LIST_BOUND_ROWS,
    ids=[r[0] for r in LIST_BOUND_ROWS],
)
def test_list_memories_never_pages_past_its_window_and_carries_the_stop(
    world, monkeypatch, caplog, case, on, store, page, names, warnings
):
    if store == "real":
        seed_many(world.mem, 201)
        kw = {}
    else:
        world.backend._mem = ListOverReturn()
        kw = {"metadata_key": "marker", "metadata_value": "yes"}
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, on):
        got = world.backend.list_memories(limit=4, offset=0, **kw)
    if isinstance(page, int):
        assert len(got) == page
    else:
        assert [r["item_id"] for r in got] == page
    assert getattr(got, "partial", ()) == names
    assert type(got).__name__ == (
        "PartialRows" if names else ("_FrozenListPage" if on else "list")
    )
    assert (answer_log(caplog) if store == "real" else full_log(caplog)) == warnings
    if store == "real":
        # through the tool: the model-facing page names the stop (ON), the legacy page does not (OFF)
        caplog.clear()
        with switch_override(SWITCH, on):
            out = world.tools["memory_list"](limit=4, offset=0)
        assert "The answer was partial (fetch_bound)" not in out
        assert ("of 201 memories" in out) is on


# -- memory_list and R11, clause by clause ----------------------------------------------------------------------------
# R11 (a) no existence oracle: a hidden row and an absent row give the same page (row below).
# R11 (b)/(c) N/A: memory_list has no cursor token. Paging is ``limit``/``offset`` over one fresh wildcard read.
# R11 (d) MET (LOCAL-FROZEN-PAGE-1): gate ON, the list pages on core's frozen walk (``read_gated_roots_page``). The
#     backend keeps the cursor that continues each page it served, so ``limit``/``offset`` is unchanged and a row written
#     or deleted between pages never changes the membership or the total (``test_r11d_paging_is_frozen_across_pages``).
# R11 (e) a partial page is never "no more memories": ``LIST_ROWS`` "partial page past the end" and the window rows above.
# R11 (f) inherited, accepted by the coordinator: ``limit=0`` is accepted by the unchanged code (not changed here).


def _listed(world, **kw):
    with switch_override(SWITCH, True):
        return [r["item_id"] for r in world.backend.list_memories(**kw)]


def test_r11a_a_hidden_row_and_an_absent_row_give_the_same_page(world, caplog):
    seed(
        world.mem, "semantic", visible={1, 2, 3, 4, 5}
    )  # row 0 exists, retired: hidden by the gate
    caplog.set_level(logging.WARNING)
    hidden = _listed(world, limit=10, offset=0)
    hidden_log = answer_log(caplog)
    world.mem._graph.backend.remove_node("test_u521_semantic_0")
    caplog.clear()
    absent = _listed(world, limit=10, offset=0)
    assert hidden == absent and "test_u521_semantic_0" not in hidden
    assert hidden_log == answer_log(caplog) == []


def _write_new_row(world, _expected):
    world.mem._graph.backend.add_node(
        "test_u521_semantic_new",
        {
            "content": "atlas budget new",
            "origin": "user:test",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        memory_type="semantic",
    )


def _delete_member(world, expected):
    world.mem._graph.backend.remove_node(expected[4])


# One row per paging shape. (case, limit, offsets walked in order, what lands after page one, expected rows per page as
# slices of the walk's membership, a member id index that never appears, the frozen total every page carries)
R11D_ROWS = [
    ("first page", 4, [0], None, [(0, 4)], None, 6),
    ("middle page", 2, [0, 2], None, [(0, 2), (2, 4)], None, 6),
    ("last page", 4, [0, 4], None, [(0, 4), (4, 6)], None, 6),
    ("empty page past the end", 4, [0, 9], None, [(0, 4), (0, 0)], None, 6),
    (
        "a write between pages",
        3,
        [0, 3],
        _write_new_row,
        [(0, 3), (3, 6)],
        None,
        6,
    ),
    (
        "a delete between pages",
        3,
        [0, 3],
        _delete_member,
        [(0, 3), (3, 6)],
        4,
        6,
    ),
]


@pytest.mark.parametrize(
    "case,limit,offsets,mutate,slices,dropped,total",
    R11D_ROWS,
    ids=[r[0] for r in R11D_ROWS],
)
def test_r11d_paging_is_frozen_across_pages(
    world, case, limit, offsets, mutate, slices, dropped, total
):
    """R11(d): membership and total are the walk's first-page values, whatever lands between pages."""
    seed(world.mem, "semantic", visible={0, 1, 2, 3, 4, 5})
    expected = _listed(world, limit=10, offset=0)  # a walk of its own: the fixed membership
    assert len(expected) == 6
    pages = []
    with switch_override(SWITCH, True):
        for n, offset in enumerate(offsets):
            page = world.backend.list_memories(limit=limit, offset=offset)
            pages.append(([r["item_id"] for r in page], page.total))
            if n == 0 and mutate is not None:
                mutate(world, expected)
    seen = [i for ids, _ in pages for i in ids]
    gone = expected[dropped] if dropped is not None else None
    for (ids, got_total), (lo, hi) in zip(pages, slices):
        assert ids == [i for i in expected[lo:hi] if i != gone]
        assert got_total == total
    assert len(seen) == len(set(seen)) and "test_u521_semantic_new" not in seen


def test_r11d_unseen_offset_starts_a_walk_and_skips(world):
    """A client that asks for offset 3 with no earlier page on record gets rows 3.. of a new walk, then can continue."""
    seed(world.mem, "semantic", visible={0, 1, 2, 3, 4, 5})
    expected = _listed(world, limit=10, offset=0)
    world.backend._walks.clear()
    with switch_override(SWITCH, True):
        page = world.backend.list_memories(limit=2, offset=3)
        nxt = world.backend.list_memories(limit=2, offset=5)
    assert [r["item_id"] for r in page] == expected[3:5] and page.total == 6
    assert [r["item_id"] for r in nxt] == expected[5:6] and nxt.total == 6


def test_r11d_the_tool_prints_the_frozen_total_on_every_page(world, caplog):
    seed(world.mem, "semantic", visible={0, 1, 2, 3, 4, 5})
    caplog.set_level(logging.WARNING)
    first = call(world, "memory_list", True, limit=3, offset=0)
    _write_new_row(world, None)
    second = call(world, "memory_list", True, limit=3, offset=3)
    assert "Showing 3 of 6 memories" in first and "Showing 3 of 6 memories" in second
    assert "test_u521_semantic_new" not in second
    assert LIST_TOTAL_LOST not in answer_log(caplog)


# -- transcript_search (already read core's coverage before this round; kept on the real core) ----------------------


@pytest.fixture
def transcripts(world, tmp_path, monkeypatch):
    transcript_tools.reset_memory()
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", str(tmp_path))
    monkeypatch.setattr(transcript_tools, "_get_memory", lambda: world.mem)
    monkeypatch.setattr(transcript_tools, "_index_dimension", lambda _d: 384)
    monkeypatch.setattr(transcript_tools, "_query_dimension", lambda: 384)
    tools = {}

    class Registration:
        def tool(self, *a, **kw):
            def decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return decorate

    transcript_tools.register(Registration())
    yield tools["transcript_search"]
    transcript_tools.reset_memory()


TRANSCRIPT_NOTE = (
    "The search was partial (core reported: complete, read_gate_scan_budget_spent), so matching sessions past what it "
    "examined may not be shown."
)


def test_transcript_search_partial_empty_and_off_arm(world, transcripts, caplog):
    seed(
        world.mem,
        "semantic",
        visible={5},
        origin="import:claude_code",
        extra={"title": "atlas"},
    )
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True):
        on = transcripts("atlas budget")
    assert TRANSCRIPT_NOTE in on
    assert answer_log(caplog) == [gate_warning("transcript search")]
    caplog.clear()
    with switch_override(SWITCH, False):
        off = transcripts("atlas budget")
    assert "The search was partial" not in off
    assert answer_log(caplog) == [OFF_SWITCH_WARNING]


# (case, gate on, how the answer starts, the WARNING list minus NOISE). The stand-in is the real search's own rows
# repeated three times: a callee that returns rows past the fetch it was given. ON: only the fetch is shown and core's
# WARNING says so; OFF: the legacy answer shows all of them.
TRANSCRIPT_OVER_ROWS = [
    (
        "on is cut to the fetch",
        True,
        "2 session(s) matching",
        [over("transcript search", 6, 2)],
    ),
    ("off is the legacy answer", False, "6 session(s) matching", [OFF_SWITCH_WARNING]),
]


@pytest.mark.parametrize(
    "case,on,shown,warnings",
    TRANSCRIPT_OVER_ROWS,
    ids=[r[0] for r in TRANSCRIPT_OVER_ROWS],
)
def test_transcript_search_never_shows_a_hit_past_its_fetch(
    world, transcripts, monkeypatch, caplog, case, on, shown, warnings
):
    from smartmemory.search.results import SearchDiagnostics, SearchResults

    seed(
        world.mem,
        "semantic",
        visible={0, 1, 2, 3, 4, 5},
        origin="import:claude_code",
        extra={"title": "atlas"},
    )
    real = world.mem.search

    def over_return(query, *, top_k, **kw):
        return SearchResults(
            list(real(query, top_k=top_k, **kw)) * 3,
            SearchDiagnostics(coverage={"complete": True}),
        )

    monkeypatch.setattr(world.mem, "search", over_return)
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, on):
        out = transcripts("atlas budget", top_k=2)
    assert out.startswith(shown)
    assert answer_log(caplog) == warnings


# The project filter runs AFTER the fetch (U5.21 fix round 3, C3/C8): a full fetch is a stop at the bound, decided from the
# rows fetched, never from how many the filter kept. One row per case of the class. top_k=2, so a project fetch is 16.
# (case, gate on, project, rows that match the project, rows from another project, rows with no cwd, core coverage,
#  the answer's notes after the hits, the entire WARNING list). The 17-row cases are a callee that over-returns by one.
BOUND_FETCH = 16
BOUND_NOTE = (
    f"\nFiltered from the top {BOUND_FETCH} semantic matches; a session ranked below that is not shown. Raise top_k to "
    "widen the window."
)
UNKNOWN_NOTE = (
    "\n1 candidate(s) carried no recorded working directory (imported before provenance) and could not be matched "
    "\u2014 re-import to make them filterable."
)
PARTIAL_NOTE = "\nThe search was partial (core reported: complete), so matching sessions past what it examined may not be shown."
TRANSCRIPT_BOUND_ROWS = [
    # the reviewer's r3 F1 reproduction: the one matching row is ranked past the fetch, so it is cut and not used
    (
        "on over-return cut then no match is empty with the bound",
        True,
        "/test_project",
        1,
        16,
        0,
        {"complete": True},
        BOUND_NOTE,
        [over("transcript search", 17, BOUND_FETCH)],
    ),
    (
        "on full fetch and no match is empty with the bound",
        True,
        "/test_project",
        0,
        16,
        0,
        {"complete": True},
        BOUND_NOTE,
        [],
    ),
    (
        "on full fetch and a short answer carries the bound",
        True,
        "/test_project",
        1,
        15,
        0,
        {"complete": True},
        BOUND_NOTE,
        [],
    ),
    (
        "on full fetch and a full answer carries the bound",
        True,
        "/test_project",
        2,
        14,
        0,
        {"complete": True},
        BOUND_NOTE,
        [],
    ),
    (
        "on fetch not full and no match is the plain empty answer",
        True,
        "/test_project",
        0,
        15,
        0,
        {"complete": True},
        "",
        [],
    ),
    (
        "on full fetch with an unmatched row keeps both notes",
        True,
        "/test_project",
        0,
        15,
        1,
        {"complete": True},
        UNKNOWN_NOTE + BOUND_NOTE,
        [],
    ),
    (
        "on full fetch and core partial keeps both explanations",
        True,
        "/test_project",
        0,
        16,
        0,
        {"complete": False},
        BOUND_NOTE + PARTIAL_NOTE,
        [gate_warning("transcript search", "complete")],
    ),
    (
        "on no project filter never claims a bound",
        True,
        "",
        2,
        0,
        0,
        {"complete": True},
        "",
        [],
    ),
    (
        "off full fetch and a short answer is the legacy answer",
        False,
        "/test_project",
        1,
        15,
        0,
        {"complete": True},
        "",
        [],
    ),
    (
        "off over-return is not cut and the legacy answer shows the ninth",
        False,
        "/test_project",
        1,
        16,
        0,
        {"complete": True},
        "",
        [],
    ),
]


@pytest.mark.parametrize(
    "case,on,project,match,other,nocwd,coverage,notes,warnings",
    TRANSCRIPT_BOUND_ROWS,
    ids=[r[0] for r in TRANSCRIPT_BOUND_ROWS],
)
def test_transcript_search_project_filter_keeps_the_fetch_bound_explanation(
    world,
    transcripts,
    monkeypatch,
    caplog,
    tmp_path,
    case,
    on,
    project,
    match,
    other,
    nocwd,
    coverage,
    notes,
    warnings,
):
    import types

    from smartmemory.search.results import SearchDiagnostics, SearchResults

    def row(item_id, cwd):
        meta = {"title": "atlas"} | ({"cwd": cwd} if cwd else {})
        return types.SimpleNamespace(item_id=item_id, content="atlas", metadata=meta)

    # other-project rows rank first, the matching ones after them, the rows with no cwd last
    rows = (
        [row(f"test_transcript_o{i}", "/other") for i in range(other)]
        + [row(f"test_transcript_m{i}", "/test_project") for i in range(match)]
        + [row(f"test_transcript_n{i}", None) for i in range(nocwd)]
    )
    monkeypatch.setattr(
        world.mem,
        "search",
        lambda query, *, top_k, **kw: SearchResults(
            list(rows), SearchDiagnostics(coverage=coverage)
        ),
    )
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, on):
        out = transcripts("atlas", top_k=2, project=project)

    fetch = BOUND_FETCH if project else 2
    # the rows the answer may show: gate ON never uses a row past the fetch
    shown = [r for r in (rows[:fetch] if on else rows) if r.metadata.get("cwd")]
    if project:
        shown = [r for r in shown if r.metadata["cwd"] == "/test_project"]
    shown = shown[:2]
    scope = "source=all" + (f", project={project}" if project else "")
    if shown:
        lines = [f"{len(shown)} session(s) matching 'atlas' ({scope}):", ""]
        for i, r in enumerate(shown, 1):
            lines += [
                f"{i}. [?] atlas",
                "   atlas",
                f"   ran in: {r.metadata['cwd']}",
                f"   item_id: {r.item_id}",
                "",
            ]
        expected = "\n".join(lines).rstrip() + notes
    else:
        expected = (
            f"No matching sessions for 'atlas' ({scope}).\n"
            f"Store: {tmp_path}. Run `transcript_status()` to see how much of the "
            f"corpus has actually been imported \u2014 a partial import is the usual "
            f"reason a real memory is missing.{notes}"
        )
    assert out == expected
    assert full_log(caplog) == warnings


def test_transcript_search_unreadable_coverage_is_a_typed_refusal_not_complete(
    world, transcripts, monkeypatch, caplog
):
    """C8: a coverage core could not have written is refused with the typed error, and logged once by its origin."""
    from smartmemory.search.results import (
        PartialCoverageUnreadable,
        SearchDiagnostics,
        SearchResults,
    )

    seed(
        world.mem,
        "semantic",
        visible={5},
        origin="import:claude_code",
        extra={"title": "atlas"},
    )
    monkeypatch.setattr(
        world.mem,
        "search",
        lambda *a, **k: SearchResults(
            [], SearchDiagnostics(coverage={"complete": "no"})
        ),
    )
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True), pytest.raises(PartialCoverageUnreadable):
        transcripts("atlas budget")
    # a refusal row: the COMPLETE list, every logger (the stand-in callee logs nothing of its own)
    assert full_log(caplog) == [
        (
            RESULTS_LOG,
            "WARNING",
            "Search coverage refused: coverage['complete'] is a str, not a bool, so the result cannot be read as "
            "complete or partial",
        )
    ]


def test_local_backend_search_carries_core_partial_names_and_off_is_a_plain_list(world):
    """The normalizer hop: ``LocalBackend.search`` is a plain list to every caller, and the call's completeness rides
    on it (ON). OFF is the plain ``list`` it always was. The per-row ``read_status`` carrier is U5.22's, not changed."""
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    with switch_override(SWITCH, True):
        on = world.backend.search("atlas budget", top_k=3)
    assert on == [] and type(on).__name__ == "PartialRows"
    assert on.partial == ("complete", "read_gate_scan_budget_spent")
    with switch_override(SWITCH, False):
        off = world.backend.search("atlas budget", top_k=3)
    assert type(off) is list and len(off) == 3
