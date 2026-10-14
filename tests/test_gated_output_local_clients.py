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
# The loggers this unit's code writes through. The facade also logs environment lines (reranker, Redis) that are not
# this unit's, so the exact list is read from these loggers only.
UNIT_LOGGERS = {
    LANE_LOG,
    GATE_LOG,
    MEMORY_TOOLS_LOG,
    "smartmemory_mcp.backends.local",
    "smartmemory_app.storage",
}

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
    },
    "reasoning": {"steps": ["a"]},
    "pending": {},
}
PARTIAL_PHRASE = "The answer was partial (core reported:"
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


def unit_log(caplog):
    return [
        (r.name, r.levelname, r.getMessage())
        for r in caplog.records
        if r.name in UNIT_LOGGERS
    ]


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
    assert [t for t in unit_log(caplog) if t[0] == GATE_LOG] == [
        (
            GATE_LOG,
            "WARNING",
            "local search returned a partial answer under the read gate: core reported "
            "complete, read_gate_scan_budget_spent, so visible matches past what it examined are not shown",
        )
    ]
    assert [t[0] for t in unit_log(caplog)].count(
        LANE_LOG
    ) >= 1  # core's own budget WARNING precedes the client's


def test_f1_all_visible_control_says_nothing_partial(world, caplog):
    """The control: every row visible, the same budget. Core still reports partial (more candidates unexamined), but
    the visible rows are shown with the partial note, and with a roomy budget there is no note at all."""
    seed(world.mem, "semantic", visible={0, 1, 2, 3, 4, 5})
    caplog.set_level(logging.WARNING)
    roomy = call(world, "memory_search", True, query="atlas budget", top_k=3)
    assert PARTIAL_PHRASE not in roomy and "test_u521_semantic_0" in roomy
    assert [t for t in unit_log(caplog) if t[0] in (LANE_LOG, GATE_LOG)] == []
    world.set_budget(1)
    caplog.clear()
    tight = call(world, "memory_search", True, query="atlas budget", top_k=3)
    assert (
        "test_u521_semantic_0" in tight
        and PARTIAL_PHRASE in tight
        and "No visible" not in tight
    )


def test_f1_off_arm_is_the_legacy_answer(world, caplog):
    """OFF: no gate, no coverage read, no note, no client WARNING: rows the gate would hide are the legacy rows."""
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    off = call(world, "memory_search", False, query="atlas budget", top_k=3)
    assert (
        PARTIAL_PHRASE not in off
        and "No visible" not in off
        and "test_u521_semantic_0" in off
    )
    assert [t for t in unit_log(caplog) if t[0] in (LANE_LOG, GATE_LOG)] == [
        OFF_SWITCH_WARNING
    ]


# -- every other consumer of a gated search: one row per consumer (class: normalizer/consumer drops completeness) -----

# (case, tool, kwargs, memory_type seeded, legacy text prefix OFF, noun in the ON refusal)
CONSUMER_ROWS = [
    (
        "dev_query_decisions",
        "dev_query_decisions",
        {"query": "atlas budget"},
        "decision",
        "Found ",
        "decisions",
    ),
    (
        "dev_load_context sessions",
        "dev_load_context",
        {"topic": "atlas budget"},
        "episodic",
        "=== Recent",
        "recent sessions",
    ),
    (
        "code_search",
        "code_search",
        {"query": "atlas budget"},
        "code",
        "Found ",
        "code entities",
    ),
    (
        "reasoning_query_traces",
        "reasoning_query_traces",
        {"query": "atlas budget"},
        "reasoning",
        "Found ",
        "reasoning traces",
    ),
]


@pytest.mark.parametrize(
    "case,tool,kw,mtype,off_prefix,noun",
    CONSUMER_ROWS,
    ids=[r[0] for r in CONSUMER_ROWS],
)
def test_every_consumer_of_a_partial_empty_search_says_partial_not_none(
    world, case, tool, kw, mtype, off_prefix, noun
):
    seed(world.mem, mtype, visible={5})
    world.set_budget(1)
    on = call(world, tool, True, **kw)
    assert f"No visible {noun} in the part core examined." in on
    assert PARTIAL_PHRASE in on and NO_VISIBLE in on
    off = call(world, tool, False, **kw)
    assert off.startswith(off_prefix) and PARTIAL_PHRASE not in off


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


# -- memory_list: the paginated read (R11) --------------------------------------------------------------------------

# (case, rows visible, scan budget, limit, offset, ids shown in order, partial note expected)
# Pages are cut from rows core's gate already admitted, there is no cursor token (offset/limit only), and ``limit`` and
# ``offset`` bound the page under both coverage states. ``limit=0`` is accepted by the inherited, unchanged code.
LIST_ROWS = [
    (
        "complete first page is bounded by limit",
        {0, 1, 2, 3, 4, 5},
        None,
        4,
        0,
        [0, 1, 2, 3],
        False,
    ),
    ("complete second page by offset", {0, 1, 2, 3, 4, 5}, None, 4, 4, [4, 5], False),
    (
        "complete page past the end is empty and silent",
        {0, 1, 2, 3, 4, 5},
        None,
        4,
        9,
        [],
        False,
    ),
    ("partial page names itself", {5}, 1, 4, 0, [5], True),
    ("partial page past the end is not 'no more memories'", {5}, 1, 4, 9, [], True),
]


@pytest.mark.parametrize(
    "case,visible,budget,limit,offset,shown,note",
    LIST_ROWS,
    ids=[r[0] for r in LIST_ROWS],
)
def test_memory_list_pages_under_both_coverage_states(
    world, caplog, case, visible, budget, limit, offset, shown, note
):
    seed(world.mem, "semantic", visible=visible)
    if budget is not None:
        # The wildcard "*" refills until it is complete on its own: with one memory type it never reports the budget
        # spent. A second type with a visible row is what makes core's merged enumeration cross the budget and say so.
        seed(world.mem, "episodic", visible={5})
    world.set_budget(budget)
    caplog.set_level(logging.WARNING)
    out = call(world, "memory_list", True, limit=limit, offset=offset)
    listed = out.count("- [test_u521_")
    assert listed == len(shown)
    if not note:  # one memory type is seeded, so the rows are exactly the semantic ones named in ``shown``
        assert [n for n in range(6) if f"test_u521_semantic_{n}]" in out] == shown
    assert (PARTIAL_PHRASE in out) is note
    if note and not shown:
        assert (
            "No visible memories in the part core examined." in out
            and NO_VISIBLE in out
        )
    assert [t for t in unit_log(caplog) if t[0] == MEMORY_TOOLS_LOG] == [
        (
            MEMORY_TOOLS_LOG,
            "WARNING",
            "Memory list total is unavailable; corpus size was lost because the backend returned only a page list.",
        )
    ]


def test_memory_list_off_arm_is_the_legacy_page(world, caplog):
    seed(world.mem, "semantic", visible={5})
    world.set_budget(1)
    caplog.set_level(logging.WARNING)
    out = call(world, "memory_list", False, limit=4, offset=0)
    assert PARTIAL_PHRASE not in out and "test_u521_semantic_0]" in out
    assert [t for t in unit_log(caplog) if t[0] in (LANE_LOG, GATE_LOG)] == [
        OFF_SWITCH_WARNING
    ]


# (case, what makes list_memories raise, WARNINGs from smartmemory.search.results, WARNINGs from memory_tools). A core
# refusal is logged once by its origin (the mark on the exception), an unmarked failure is logged by the tool as before.
LIST_REFUSAL_ROWS = [
    (
        "core refusal marked by its origin is not logged a second time",
        "unreadable coverage",
        1,
        0,
    ),
    ("unmarked backend failure is still logged by the tool", "boom", 0, 1),
]


@pytest.mark.parametrize(
    "case,fault,from_core,from_tool",
    LIST_REFUSAL_ROWS,
    ids=[r[0] for r in LIST_REFUSAL_ROWS],
)
def test_memory_list_logs_a_refusal_once(
    world, monkeypatch, caplog, case, fault, from_core, from_tool
):
    from smartmemory.search.results import (
        PartialCoverageUnreadable,
        SearchDiagnostics,
        SearchResults,
    )

    seed(world.mem, "semantic", visible={5})
    if fault == "boom":
        monkeypatch.setattr(
            world.backend,
            "list_memories",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        expected = RuntimeError
    else:
        monkeypatch.setattr(
            world.mem,
            "search",
            lambda *a, **k: SearchResults(
                [], SearchDiagnostics(coverage={"complete": "no"})
            ),
        )
        expected = PartialCoverageUnreadable
    caplog.set_level(logging.WARNING)
    with switch_override(SWITCH, True), pytest.raises(expected):
        world.tools["memory_list"](limit=4, offset=0)
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert (
        sum(r.name == "smartmemory.search.results" for r in warned),
        sum(r.name == MEMORY_TOOLS_LOG for r in warned),
    ) == (from_core, from_tool)


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
    assert [t[2] for t in unit_log(caplog) if t[0] == GATE_LOG] == [
        "transcript search returned a partial answer under the read gate: core reported "
        "complete, read_gate_scan_budget_spent, so visible matches past what it examined are not shown"
    ]
    caplog.clear()
    with switch_override(SWITCH, False):
        off = transcripts("atlas budget")
    assert "The search was partial" not in off
    assert [t for t in unit_log(caplog) if t[0] in (LANE_LOG, GATE_LOG)] == [
        OFF_SWITCH_WARNING
    ]


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
    assert [
        (r.name, r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == "smartmemory.search.results"
    ] == [
        (
            "smartmemory.search.results",
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
