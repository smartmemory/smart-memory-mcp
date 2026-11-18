"""The save tools (U4.42-FU-TOOLS, G56/G140): ``memory_save``, ``memory_save_document``, ``memory_save_conversation``.

One explicit row table per surface (no stacked ``parametrize``):

* ``TOOL_ROWS``    what each tool returns for each backend return arm, and which backend method it called.
* ``REFUSAL_ROWS`` input the tools refuse before touching the backend, and a typed refusal that propagates.
* ``ROUTING_ROWS`` pair-vs-transcript routing and the ``pending`` raw doorway.
* ``WIRE_ROWS``    ``read_wire_save`` against the contract's examples (``SM2-SAVE-1/save-receipt-contract.json``,
                   copied as literals: the docs repo is not in this tree).
* ``REMOTE_ROWS``  the same wire read through ``RemoteBackend`` with a fake ``_request``.
* ``HOSTED_ROWS`` / ``GONE_NAMES``  the hosted allowlist and the G166 negative test (six old names gone, no aliases).
"""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from smartmemory_mcp.backends.local import SaveReturn
from smartmemory_mcp.backends.receipts import (
    WireSaveRefused,
    WireSaveRejected,
    read_wire_save,
)
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS
from smartmemory_mcp.tools import save_tools

MCP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The six tool names G140 removed at 2.0.0 with no aliases.
GONE_NAMES = (
    "memory_ingest",
    "memory_add",
    "memory_distill",
    "memory_ingest_conversation",
    "memory_ingest_structured",
    "memory_ingest_document",
)


class FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


class RecordingBackend:
    """Every save method records ``(name, args, kwargs)`` and returns the preset ``SaveReturn``."""

    METHODS = (
        "add_with_receipt",
        "ingest_with_receipt",
        "ingest_structured_with_receipt",
        "ingest_document_with_receipt",
        "ingest_conversation_with_receipt",
    )

    def __init__(self, ret: SaveReturn):
        self.ret = ret
        self.calls: list[tuple[str, tuple, dict]] = []
        for name in self.METHODS:
            setattr(self, name, self._make(name))

    def _make(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.ret

        return method


def _tools(monkeypatch, ret: SaveReturn):
    backend = RecordingBackend(ret)
    monkeypatch.setattr(save_tools, "get_backend", lambda: backend)
    mcp = FakeMCP()
    save_tools.register_free(mcp)
    save_tools.register_pro(mcp)
    return mcp.fns, backend


# --- contract examples, copied from SM2-SAVE-1/save-receipt-contract.json ---------------------------------------

_ITEM = {"kind": "item_save", "item_id": "mem_123"}
EX_QUEUED = {  # examples[0]
    "schema_version": "1.1",
    "subject": _ITEM,
    "run_id": "run_1",
    "status_url": "/memory/status/run_1",
    "processing_stage": "received",
    "execution_state": "queued",
    "outcome": None,
    "terminal": False,
    "result_availability": "pending",
}
EX_COMPLETED = {  # examples[1]
    "schema_version": "1.1",
    "subject": _ITEM,
    "run_id": "run_1",
    "status_url": "/memory/status/run_1",
    "processing_stage": "extracted",
    "execution_state": "completed",
    "outcome": "completed",
    "terminal": True,
    "result_availability": "available",
}
EX_CONFLICT = {  # examples[2]
    "schema_version": "1.1",
    "subject": {"kind": "edit", "item_id": "mem_123"},
    "admission_error": {
        "code": "revision_conflict",
        "message": "This item changed since it was opened; review the current version before retrying.",
    },
}
EX_SKIPPED = {  # examples[5]
    "schema_version": "1.1",
    "subject": {"kind": "item_save", "item_id": "local_1"},
    "run_id": "local_run_1",
    "status_url": "local-status:local_run_1",
    "processing_stage": "stored",
    "execution_state": "completed",
    "outcome": "skipped",
    "terminal": True,
}
EX_IDEMPOTENCY = {  # examples[6]
    "schema_version": "1.1",
    "admission_error": {
        "code": "idempotency_conflict",
        "message": "This retry key is bound to a different request.",
    },
}
EX_EXPIRED_KEY = {  # examples[7]
    "schema_version": "1.1",
    "admission_error": {
        "code": "retry_key_expired",
        "message": "Retry key expired. Cannot match. Keep the draft and confirm a new save.",
    },
}
EX_PROJECTION = {  # examples[8]: no subject, so no id this reader may use
    "schema_version": "1.1",
    "projection_kind": "legacy_item_projection",
    "item_id": "mem_legacy",
    "revision": 7,
    "status_provenance": "unknown",
}
EX_PAUSED = {  # examples[10]
    "schema_version": "1.1",
    "subject": {"kind": "item_save", "item_id": "mem_paused"},
    "run_id": "run_pause_1",
    "status_url": "/memory/status/run_pause_1",
    "processing_stage": "stored",
    "execution_state": "paused",
    "reason": "allowance",
    "outcome": None,
    "terminal": False,
}
EX_RUNNING = {  # examples[17]
    "schema_version": "1.1",
    "subject": {"kind": "item_save", "item_id": "mem_reset"},
    "run_id": "run_wait",
    "status_url": "/memory/status/run_wait",
    "processing_stage": "stored",
    "execution_state": "running",
    "outcome": None,
    "terminal": False,
}
EX_PARTIAL_DOC = {  # examples[14]
    "schema_version": "1.1",
    "subject": {"kind": "document_save", "document_id": "doc_partial"},
    "run_id": "run_doc_1",
    "status_url": "/memory/status/run_doc_1",
    "processing_stage": "stored",
    "execution_state": "completed",
    "outcome": "partial",
    "terminal": True,
    "missing": ["chunk_2"],
}
EX_DEAD = {  # examples[16]
    "schema_version": "1.1",
    "subject": {"kind": "item_save", "item_id": "mem_reset"},
    "run_id": "run_old",
    "status_url": "/memory/status/run_old",
    "processing_stage": "extracted",
    "execution_state": "dead",
    "outcome": "dead",
    "terminal": True,
}
EX_GENERATION = {
    "schema_version": "1.0",
    "subject": {"kind": "generation_save", "generation_id": "gen_9"},
    "run_id": "run_gen",
    "processing_stage": "stored",
    "execution_state": "completed",
    "outcome": "completed",
    "terminal": True,
}


def _wire(base: dict, **changes) -> dict:
    return {**base, **changes}


def _receipt_return(wire: dict, item_id: str, summary: str) -> SaveReturn:
    return SaveReturn(item_id, wire, summary)


HELD = {"status": "held", "reason": "low_confidence", "reasons": ["r1", "r2"]}

# --- TOOL_ROWS ------------------------------------------------------------------------------------------------
# (label, tool, kwargs, backend return, backend method called, status, text fragment, structured subset)
TOOL_ROWS = [
    (
        "item legacy id",
        "memory_save",
        {"content": "hi"},
        SaveReturn("id-1"),
        "ingest_with_receipt",
        "saved",
        "Memory saved. Item ID: id-1",
        {"item_id": "id-1", "receipt": None},
    ),
    (
        "item legacy dict with item_id",
        "memory_save",
        {"content": "hi"},
        SaveReturn({"item_id": "id-2", "extra": 1}),
        "ingest_with_receipt",
        "saved",
        "Item ID: id-2",
        {"item_id": "id-2"},
    ),
    (
        "item legacy dict with id key",
        "memory_save",
        {"content": "hi"},
        SaveReturn({"id": "id-3"}),
        "ingest_with_receipt",
        "saved",
        "Item ID: id-3",
        {"item_id": "id-3"},
    ),
    (
        "item legacy dict with no id",
        "memory_save",
        {"content": "hi"},
        SaveReturn({"something": 1}),
        "ingest_with_receipt",
        "saved",
        "No item id returned",
        {"item_id": None},
    ),
    (
        "item held",
        "memory_save",
        {"content": "hi"},
        SaveReturn(HELD),
        "ingest_with_receipt",
        "held",
        "Memory held: low_confidence. Details: ['r1', 'r2']",
        {"reason": "low_confidence", "reasons": ["r1", "r2"]},
    ),
    (
        "item error dict",
        "memory_save",
        {"content": "hi"},
        SaveReturn({"error": "boom"}),
        "ingest_with_receipt",
        "failed",
        "Memory save failed: boom",
        {"error": "boom"},
    ),
    (
        "item receipt queued",
        "memory_save",
        {"content": "hi"},
        _receipt_return(EX_QUEUED, "mem_123", "mem_123 (Saving)"),
        "ingest_with_receipt",
        "queued",
        "Memory: mem_123 (Saving)",
        {"item_id": "mem_123", "receipt": EX_QUEUED, "summary": "mem_123 (Saving)"},
    ),
    (
        "item receipt completed",
        "memory_save",
        {"content": "hi"},
        _receipt_return(EX_COMPLETED, "mem_123", "mem_123 (Saved)"),
        "ingest_with_receipt",
        "saved",
        "Memory: mem_123 (Saved)",
        {"item_id": "mem_123"},
    ),
    (
        "item receipt dead",
        "memory_save",
        {"content": "hi"},
        _receipt_return(EX_DEAD, "mem_reset", "mem_reset (Failed)"),
        "ingest_with_receipt",
        "failed",
        "mem_reset (Failed)",
        {"item_id": "mem_reset"},
    ),
    (
        "item receipt failed state, not terminal",
        "memory_save",
        {"content": "hi"},
        _receipt_return(_wire(EX_RUNNING, execution_state="failed"), "mem_reset", "x"),
        "ingest_with_receipt",
        "failed",
        "Memory: x",
        {},
    ),
    (
        "item receipt skipped is saved",
        "memory_save",
        {"content": "hi"},
        _receipt_return(EX_SKIPPED, "local_1", "local_1 (Saved, extraction skipped)"),
        "ingest_with_receipt",
        "saved",
        "extraction skipped",
        {"item_id": "local_1"},
    ),
    (
        "item pending goes raw, id arm",
        "memory_save",
        {"content": "hi", "memory_type": "pending"},
        SaveReturn("id-4"),
        "add_with_receipt",
        "saved",
        "Memory saved. Item ID: id-4",
        {"item_id": "id-4"},
    ),
    (
        "item pending held",
        "memory_save",
        {"content": "hi", "memory_type": "pending"},
        SaveReturn(HELD),
        "add_with_receipt",
        "held",
        "Memory held: low_confidence",
        {"reasons": ["r1", "r2"]},
    ),
    (
        "structured legacy id",
        "memory_save",
        {"data": {"k": 1}, "memory_type": "test_schema"},
        SaveReturn("sid-1"),
        "ingest_structured_with_receipt",
        "saved",
        "Structured item saved. Schema: test_schema. Item ID: sid-1",
        {"item_id": "sid-1"},
    ),
    (
        "structured receipt",
        "memory_save",
        {"data": {"k": 1}, "memory_type": "test_schema"},
        _receipt_return(EX_COMPLETED, "mem_123", "mem_123 (Saved)"),
        "ingest_structured_with_receipt",
        "saved",
        "Structured item: mem_123 (Saved)",
        {"item_id": "mem_123"},
    ),
    (
        "document legacy dict",
        "memory_save_document",
        {"source": "https://example.com/a"},
        SaveReturn(
            {"document_id": "d1", "chunk_ids": ["c1", "c2"], "status": "processed"}
        ),
        "ingest_document_with_receipt",
        "saved",
        "Document processed. ID: d1, chunks: 2, status: processed",
        {
            "document_id": "d1",
            "chunk_ids": ["c1", "c2"],
            "document_status": "processed",
        },
    ),
    (
        "document legacy dict, no chunk ids",
        "memory_save_document",
        {"source": "https://example.com/a"},
        SaveReturn({"document_id": "d2", "status": "duplicate"}),
        "ingest_document_with_receipt",
        "saved",
        "chunks: 0",
        {"chunk_ids": []},
    ),
    (
        "document receipt partial",
        "memory_save_document",
        {"source": "https://example.com/a"},
        _receipt_return(
            EX_PARTIAL_DOC, "doc_partial", "doc_partial (Saved, partly processed)"
        ),
        "ingest_document_with_receipt",
        "saved",
        "Document: doc_partial (Saved, partly processed)",
        {"document_id": "doc_partial"},
    ),
    (
        "document receipt queued",
        "memory_save_document",
        {"source": "https://example.com/a"},
        _receipt_return(
            _wire(EX_QUEUED, subject={"kind": "document_save", "document_id": "d3"}),
            "d3",
            "d3 (Saving)",
        ),
        "ingest_document_with_receipt",
        "queued",
        "Document: d3 (Saving)",
        {"document_id": "d3"},
    ),
    (
        "conversation transcript legacy, all saved",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}] * 3, "conversation_id": "c9"},
        SaveReturn({"conversation_id": "c9", "chunks_ingested": 2, "chunks_failed": 0}),
        "ingest_conversation_with_receipt",
        "saved",
        "Conversation c9: 2 chunks ingested, 0 failed.",
        {"conversation_id": "c9", "chunks_ingested": 2, "chunks_held": 0},
    ),
    (
        "conversation transcript legacy, all held",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}]},
        SaveReturn(
            {
                "conversation_id": "c1",
                "chunks_ingested": 0,
                "chunks_failed": 1,
                "chunk_results": [{"status": "held", "reason": "low_confidence"}],
            }
        ),
        "ingest_conversation_with_receipt",
        "held",
        "0 chunks ingested, 0 failed. 1 held: low_confidence",
        {"chunks_held": 1, "chunks_failed": 0},
    ),
    (
        "conversation transcript legacy, all failed",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}]},
        SaveReturn(
            {
                "conversation_id": "c2",
                "chunks_ingested": 0,
                "chunks_failed": 2,
                "chunk_results": [],
            }
        ),
        "ingest_conversation_with_receipt",
        "failed",
        "0 chunks ingested, 2 failed.",
        {"chunks_failed": 2},
    ),
    (
        "conversation transcript legacy, mixed is saved",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}]},
        SaveReturn(
            {
                "conversation_id": "c3",
                "chunks_ingested": 1,
                "chunks_failed": 1,
                "chunk_results": [],
            }
        ),
        "ingest_conversation_with_receipt",
        "saved",
        "1 chunks ingested, 1 failed.",
        {"chunks_ingested": 1},
    ),
    (
        "conversation transcript error dict",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}]},
        SaveReturn({"error": "nope"}),
        "ingest_conversation_with_receipt",
        "failed",
        "Conversation save failed: nope",
        {"error": "nope"},
    ),
    (
        "conversation transcript receipt",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}], "conversation_id": "c4"},
        _receipt_return(EX_QUEUED, "mem_123", "mem_123 (Saving)"),
        "ingest_conversation_with_receipt",
        "queued",
        "Conversation: mem_123 (Saving)",
        {"conversation_id": "c4"},
    ),
    (
        "conversation pair legacy id",
        "memory_save_conversation",
        {
            "turns": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ]
        },
        SaveReturn("turn-1"),
        "add_with_receipt",
        "saved",
        "Turn saved. Item ID: turn-1",
        {"item_id": "turn-1"},
    ),
    (
        "conversation pair held",
        "memory_save_conversation",
        {
            "turns": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ]
        },
        SaveReturn(HELD),
        "add_with_receipt",
        "held",
        "Turn held: low_confidence",
        {"reason": "low_confidence"},
    ),
    (
        "conversation pair receipt",
        "memory_save_conversation",
        {
            "turns": [
                {"speaker": "User", "content": "q"},
                {"speaker": "ASSISTANT", "content": "a"},
            ]
        },
        _receipt_return(EX_COMPLETED, "mem_123", "mem_123 (Saved)"),
        "add_with_receipt",
        "saved",
        "Turn: mem_123 (Saved)",
        {"item_id": "mem_123"},
    ),
]


@pytest.mark.parametrize(
    "label,tool,kwargs,ret,method,status,text,subset",
    TOOL_ROWS,
    ids=[row[0] for row in TOOL_ROWS],
)
def test_tool_rows(monkeypatch, label, tool, kwargs, ret, method, status, text, subset):
    fns, backend = _tools(monkeypatch, ret)
    result = fns[tool](**kwargs)
    assert [c[0] for c in backend.calls] == [method]
    assert result.structured_content["status"] == status
    assert text in result.content[0].text
    for key, want in subset.items():
        assert result.structured_content[key] == want, key


# --- REFUSAL_ROWS ---------------------------------------------------------------------------------------------
# (label, tool, kwargs, exception, message fragment). The backend is never called for the first group.
REFUSAL_ROWS = [
    ("neither content nor data", "memory_save", {}, ToolError, "exactly one"),
    (
        "both content and data",
        "memory_save",
        {"content": "a", "data": {"k": 1}, "memory_type": "s"},
        ToolError,
        "exactly one",
    ),
    (
        "data without a schema",
        "memory_save",
        {"data": {"k": 1}},
        ToolError,
        "memory_type",
    ),
    (
        "data with metadata",
        "memory_save",
        {"data": {"k": 1}, "memory_type": "s", "metadata": {"a": 1}},
        ToolError,
        "apply to content",
    ),
    (
        "data with tags",
        "memory_save",
        {"data": {"k": 1}, "memory_type": "s", "tags": ["t"]},
        ToolError,
        "apply to content",
    ),
]

#: A receipt-less, id-less document or conversation is a failure to report, never a made-up success.
BAD_RETURN_ROWS = [
    (
        "document dict without id",
        "memory_save_document",
        {"source": "u"},
        SaveReturn({"status": "x"}),
        "no document id",
    ),
    (
        "document not a dict",
        "memory_save_document",
        {"source": "u"},
        SaveReturn("d1"),
        "no document id",
    ),
    (
        "conversation not a dict",
        "memory_save_conversation",
        {"turns": [{"role": "user", "content": "a"}]},
        SaveReturn("c1"),
        "unreadable",
    ),
]


@pytest.mark.parametrize(
    "label,tool,kwargs,exc,fragment", REFUSAL_ROWS, ids=[r[0] for r in REFUSAL_ROWS]
)
def test_refusals_never_reach_the_backend(
    monkeypatch, label, tool, kwargs, exc, fragment
):
    fns, backend = _tools(monkeypatch, SaveReturn("never"))
    with pytest.raises(exc, match=fragment):
        fns[tool](**kwargs)
    assert backend.calls == []


@pytest.mark.parametrize(
    "label,tool,kwargs,ret,fragment",
    BAD_RETURN_ROWS,
    ids=[r[0] for r in BAD_RETURN_ROWS],
)
def test_unreadable_returns_raise(monkeypatch, label, tool, kwargs, ret, fragment):
    fns, _ = _tools(monkeypatch, ret)
    with pytest.raises(RuntimeError, match=fragment):
        fns[tool](**kwargs)


@pytest.mark.parametrize(
    "exc",
    [
        WireSaveRejected("revision_conflict", "changed"),
        WireSaveRefused("unsupported_version", "bad major", "run_1"),
    ],
    ids=["rejected", "refused"],
)
def test_typed_wire_refusals_propagate_to_the_agent(monkeypatch, exc):
    backend = SimpleNamespace(
        ingest_with_receipt=lambda *a, **k: (_ for _ in ()).throw(exc)
    )
    monkeypatch.setattr(save_tools, "get_backend", lambda: backend)
    mcp = FakeMCP()
    save_tools.register_free(mcp)
    with pytest.raises(WireSaveRefused) as caught:
        mcp.fns["memory_save"](content="x")
    assert caught.value is exc


# --- ROUTING_ROWS ---------------------------------------------------------------------------------------------
USER, ASSISTANT = (
    {"role": "user", "content": " q "},
    {"role": "assistant", "content": " a "},
)
# (label, tool, kwargs, method, args-check, kwargs-check)
ROUTING_ROWS = [
    (
        "default type is semantic, no metadata kwarg",
        "memory_save",
        {"content": "c"},
        "ingest_with_receipt",
        ("c",),
        {"memory_type": "semantic"},
    ),
    (
        "episodic goes through the pipeline",
        "memory_save",
        {"content": "c", "memory_type": "episodic"},
        "ingest_with_receipt",
        ("c",),
        {"memory_type": "episodic"},
    ),
    (
        "pending goes raw",
        "memory_save",
        {"content": "c", "memory_type": "pending"},
        "add_with_receipt",
        ("c",),
        {"memory_type": "pending"},
    ),
    (
        "tags fold into metadata",
        "memory_save",
        {"content": "c", "metadata": {"a": 1}, "tags": ["t1", "t2"]},
        "ingest_with_receipt",
        ("c",),
        {"memory_type": "semantic", "metadata": {"a": 1, "tags": ["t1", "t2"]}},
    ),
    (
        "tags alone make metadata",
        "memory_save",
        {"content": "c", "tags": ["t1"]},
        "ingest_with_receipt",
        ("c",),
        {"metadata": {"tags": ["t1"]}},
    ),
    (
        "metadata alone passes through",
        "memory_save",
        {"content": "c", "metadata": {"a": 1}},
        "ingest_with_receipt",
        ("c",),
        {"metadata": {"a": 1}},
    ),
    (
        "structured passes the schema",
        "memory_save",
        {"data": {"k": 1}, "memory_type": "sch"},
        "ingest_structured_with_receipt",
        ({"k": 1},),
        {"schema": "sch"},
    ),
    (
        "document passes its knobs",
        "memory_save_document",
        {"source": "u", "chunk_size": 10, "reference": True},
        "ingest_document_with_receipt",
        ("u",),
        {
            "source_type": "auto",
            "chunk_size": 10,
            "chunk_strategy": "paragraph",
            "reference": True,
        },
    ),
    (
        "pair is the old distill path",
        "memory_save_conversation",
        {"turns": [USER, ASSISTANT], "conversation_id": "cid"},
        "add_with_receipt",
        ("USER: q\nASSISTANT: a",),
        {
            "memory_type": "pending",
            "metadata": {
                "role": "turn",
                "distill_version": "1",
                "conversation_id": "cid",
            },
        },
    ),
    (
        "pair without a conversation id carries none",
        "memory_save_conversation",
        {"turns": [USER, ASSISTANT]},
        "add_with_receipt",
        ("USER: q\nASSISTANT: a",),
        {"metadata": {"role": "turn", "distill_version": "1"}},
    ),
    (
        "boundaries force a transcript",
        "memory_save_conversation",
        {"turns": [USER, ASSISTANT], "session_boundaries": [0]},
        "ingest_conversation_with_receipt",
        ([USER, ASSISTANT],),
        {"session_boundaries": [0], "turns_per_chunk": 15, "max_chunk_chars": 12000},
    ),
    (
        "dates force a transcript",
        "memory_save_conversation",
        {"turns": [USER, ASSISTANT], "session_dates": ["2026-10-21"]},
        "ingest_conversation_with_receipt",
        ([USER, ASSISTANT],),
        {"session_dates": ["2026-10-21"]},
    ),
    (
        "assistant first is a transcript",
        "memory_save_conversation",
        {"turns": [ASSISTANT, USER]},
        "ingest_conversation_with_receipt",
        ([ASSISTANT, USER],),
        {},
    ),
    (
        "two users is a transcript",
        "memory_save_conversation",
        {"turns": [USER, USER]},
        "ingest_conversation_with_receipt",
        ([USER, USER],),
        {},
    ),
    (
        "one turn is a transcript",
        "memory_save_conversation",
        {"turns": [USER], "conversation_id": "c"},
        "ingest_conversation_with_receipt",
        ([USER],),
        {"conversation_id": "c"},
    ),
    (
        "three turns is a transcript",
        "memory_save_conversation",
        {"turns": [USER, ASSISTANT, USER]},
        "ingest_conversation_with_receipt",
        ([USER, ASSISTANT, USER],),
        {},
    ),
    (
        "non-string content is a transcript",
        "memory_save_conversation",
        {"turns": [USER, {"role": "assistant", "content": ["blocks"]}]},
        "ingest_conversation_with_receipt",
        None,
        {},
    ),
    (
        "turn without a role is a transcript",
        "memory_save_conversation",
        {"turns": [{"content": "q"}, ASSISTANT]},
        "ingest_conversation_with_receipt",
        None,
        {},
    ),
]


@pytest.mark.parametrize(
    "label,tool,kwargs,method,want_args,want_kwargs",
    ROUTING_ROWS,
    ids=[r[0] for r in ROUTING_ROWS],
)
def test_routing_rows(monkeypatch, label, tool, kwargs, method, want_args, want_kwargs):
    ret = SaveReturn(
        {"document_id": "d", "conversation_id": "c", "chunks_ingested": 1}
        if method.startswith(("ingest_doc", "ingest_conv"))
        else "id-x"
    )
    fns, backend = _tools(monkeypatch, ret)
    fns[tool](**kwargs)
    assert len(backend.calls) == 1
    name, args, got = backend.calls[0]
    assert name == method
    if want_args is not None:
        assert args == want_args
    for key, want in want_kwargs.items():
        assert got[key] == want, key
    if "metadata" not in want_kwargs and name in (
        "add_with_receipt",
        "ingest_with_receipt",
    ):
        assert "metadata" not in got


def test_save_does_not_mutate_the_callers_metadata(monkeypatch):
    fns, _ = _tools(monkeypatch, SaveReturn("id"))
    meta = {"a": 1}
    tags = ["t"]
    fns["memory_save"](content="c", metadata=meta, tags=tags)
    assert meta == {"a": 1} and tags == ["t"]


# --- WIRE_ROWS ------------------------------------------------------------------------------------------------
# (label, wire, ("ok", item id, summary) | ("legacy",) | (exception class, code, message fragment))
WIRE_ROWS = [
    ("legacy id string", "abc", ("legacy",)),
    ("legacy held dict", HELD, ("legacy",)),
    ("legacy dict without schema_version", {"item_id": "x"}, ("legacy",)),
    ("legacy none", None, ("legacy",)),
    ("queued", EX_QUEUED, ("ok", "mem_123", "mem_123 (Saving)")),
    ("completed", EX_COMPLETED, ("ok", "mem_123", "mem_123 (Saved)")),
    ("skipped", EX_SKIPPED, ("ok", "local_1", "local_1 (Saved, extraction skipped)")),
    (
        "paused after stored",
        EX_PAUSED,
        ("ok", "mem_paused", "mem_paused (Saved, processing)"),
    ),
    (
        "running after stored",
        EX_RUNNING,
        ("ok", "mem_reset", "mem_reset (Saved, processing)"),
    ),
    (
        "partial document",
        EX_PARTIAL_DOC,
        ("ok", "doc_partial", "doc_partial (Saved, partly processed)"),
    ),
    ("dead", EX_DEAD, ("ok", "mem_reset", "mem_reset (Failed)")),
    (
        "generation subject, minor 0",
        EX_GENERATION,
        ("ok", "gen_9", "gen_9 (Saved, processing)"),
    ),
    (
        "failed state, not terminal",
        _wire(EX_RUNNING, execution_state="failed"),
        ("ok", "mem_reset", "mem_reset (Failed)"),
    ),
    (
        "terminal failed outcome",
        _wire(EX_COMPLETED, outcome="failed"),
        ("ok", "mem_123", "mem_123 (Failed)"),
    ),
    (
        "terminal unknown outcome is not read as saved",
        _wire(EX_COMPLETED, outcome="exotic"),
        ("ok", "mem_123", "mem_123 (Saved, processing)"),
    ),
    (
        "terminal completed but only stored is not read as saved",
        _wire(EX_COMPLETED, processing_stage="stored"),
        ("ok", "mem_123", "mem_123 (Saved, processing)"),
    ),
    (
        "no_op outcome, extracted",
        _wire(EX_COMPLETED, outcome="no_op"),
        ("ok", "mem_123", "mem_123 (Saved)"),
    ),
    (
        "unknown stage, not terminal",
        _wire(EX_QUEUED, processing_stage="exotic"),
        ("ok", "mem_123", "mem_123 (Saved, processing)"),
    ),
    (
        "no stage, not terminal",
        _wire(EX_QUEUED, processing_stage=None),
        ("ok", "mem_123", "mem_123 (Saving)"),
    ),
    (
        "later minor with an unknown field stays opaque",
        _wire(EX_COMPLETED, schema_version="1.9", brand_new_field={"x": 1}),
        ("ok", "mem_123", "mem_123 (Saved)"),
    ),
    (
        "admission conflict",
        EX_CONFLICT,
        (WireSaveRejected, "revision_conflict", "changed since"),
    ),
    (
        "admission idempotency conflict",
        EX_IDEMPOTENCY,
        (WireSaveRejected, "idempotency_conflict", "different request"),
    ),
    (
        "admission retry key expired",
        EX_EXPIRED_KEY,
        (WireSaveRejected, "retry_key_expired", "Retry key expired"),
    ),
    (
        "admission error that is not a mapping",
        _wire(EX_QUEUED, admission_error="no"),
        (WireSaveRejected, "rejected", "rejected"),
    ),
    (
        "unsupported major keeps the handle",
        _wire(EX_QUEUED, schema_version="2.0"),
        (WireSaveRefused, "unsupported_version", "run_1"),
    ),
    (
        "unreadable version",
        _wire(EX_QUEUED, schema_version="x"),
        (WireSaveRefused, "unsupported_version", "not supported"),
    ),
    (
        "unsupported major without a handle",
        {"schema_version": "3.1"},
        (WireSaveRefused, "unsupported_version", "Handle: none"),
    ),
    (
        "projection with no subject",
        EX_PROJECTION,
        (WireSaveRefused, "no_subject_id", "no subject id"),
    ),
    (
        "subject without an id field",
        _wire(EX_QUEUED, subject={"kind": "item_save"}),
        (WireSaveRefused, "no_subject_id", "run_1"),
    ),
    (
        "subject not a mapping",
        _wire(EX_QUEUED, subject="mem_123"),
        (WireSaveRefused, "no_subject_id", "no subject id"),
    ),
]


@pytest.mark.parametrize("label,wire,expect", WIRE_ROWS, ids=[r[0] for r in WIRE_ROWS])
def test_read_wire_save_rows(caplog, label, wire, expect):
    if expect[0] == "legacy":
        got = read_wire_save(wire, op="add")
        assert got == SaveReturn(wire) and got.receipt is None
        return
    if expect[0] == "ok":
        got = read_wire_save(wire, op="add")
        assert (got.value, got.summary) == (expect[1], expect[2])
        assert got.receipt == wire and got.receipt is not wire
        return
    exc, code, fragment = expect
    with caplog.at_level("WARNING"), pytest.raises(exc) as caught:
        read_wire_save(wire, op="add")
    assert caught.value.code == code and fragment in str(caught.value)
    assert code in caplog.text  # one WARNING naming what was lost


def test_unsupported_version_handle_is_on_the_exception():
    with pytest.raises(WireSaveRefused) as caught:
        read_wire_save(_wire(EX_QUEUED, schema_version="2.0"), op="add")
    assert caught.value.handle == "run_1"
    assert not isinstance(caught.value, WireSaveRejected)


# --- REMOTE_ROWS ----------------------------------------------------------------------------------------------
# (label, remote method, args, kwargs, fake _request response, ("ok", value, summary) | (exception, fragment))
REMOTE_ROWS = [
    (
        "add: receipt queued",
        "add_with_receipt",
        ("c", "pending"),
        {},
        EX_QUEUED,
        ("ok", "mem_123", "mem_123 (Saving)"),
    ),
    (
        "add: legacy id",
        "add_with_receipt",
        ("c", "pending"),
        {},
        {"item_id": "i1"},
        ("ok", "i1", None),
    ),
    (
        "add: legacy held",
        "add_with_receipt",
        ("c", "pending"),
        {},
        HELD,
        ("ok", HELD, None),
    ),
    (
        "add: rejected",
        "add_with_receipt",
        ("c", "pending"),
        {},
        EX_CONFLICT,
        (WireSaveRejected, "changed since"),
    ),
    (
        "add: error dict raises",
        "add_with_receipt",
        ("c", "pending"),
        {},
        {"error": "API error 500"},
        (RuntimeError, "API error 500"),
    ),
    (
        "add: no id raises",
        "add_with_receipt",
        ("c", "pending"),
        {},
        {"ok": True},
        (RuntimeError, "no item id"),
    ),
    (
        "ingest: receipt completed",
        "ingest_with_receipt",
        ("c",),
        {},
        EX_COMPLETED,
        ("ok", "mem_123", "mem_123 (Saved)"),
    ),
    (
        "ingest: legacy dict",
        "ingest_with_receipt",
        ("c",),
        {},
        {"item_id": "i2"},
        ("ok", {"item_id": "i2"}, None),
    ),
    (
        "ingest: error dict stays a value",
        "ingest_with_receipt",
        ("c",),
        {},
        {"error": "bad"},
        ("ok", {"error": "bad"}, None),
    ),
    (
        "ingest: unsupported major",
        "ingest_with_receipt",
        ("c",),
        {},
        _wire(EX_QUEUED, schema_version="2.0"),
        (WireSaveRefused, "run_1"),
    ),
    (
        "document: receipt partial",
        "ingest_document_with_receipt",
        ("u",),
        {},
        EX_PARTIAL_DOC,
        ("ok", "doc_partial", "doc_partial (Saved, partly processed)"),
    ),
    (
        "document: legacy dict",
        "ingest_document_with_receipt",
        ("u",),
        {},
        {"document_id": "d1"},
        ("ok", {"document_id": "d1"}, None),
    ),
    (
        "document: error raises",
        "ingest_document_with_receipt",
        ("u",),
        {},
        {"error": "denied"},
        (RuntimeError, "denied"),
    ),
    (
        "conversation: receipt",
        "ingest_conversation_with_receipt",
        ([USER],),
        {},
        EX_QUEUED,
        ("ok", "mem_123", "mem_123 (Saving)"),
    ),
    (
        "conversation: legacy dict",
        "ingest_conversation_with_receipt",
        ([USER],),
        {},
        {"conversation_id": "c"},
        ("ok", {"conversation_id": "c"}, None),
    ),
    (
        "structured: not available",
        "ingest_structured_with_receipt",
        ({"k": 1},),
        {"schema": "s"},
        {},
        (NotImplementedError, "remote mode"),
    ),
]


@pytest.mark.parametrize(
    "label,method,args,kwargs,response,expect",
    REMOTE_ROWS,
    ids=[r[0] for r in REMOTE_ROWS],
)
def test_remote_backend_reads_the_wire(
    monkeypatch, label, method, args, kwargs, response, expect
):
    backend = RemoteBackend(api_key="sk_test_key_123")
    seen: list[tuple] = []

    def fake_request(*a, **k):
        seen.append((a, k))
        return response

    monkeypatch.setattr(backend, "_request", fake_request)
    if expect[0] == "ok":
        got = getattr(backend, method)(*args, **kwargs)
        assert (got.value, got.summary) == (expect[1], expect[2])
        assert (got.receipt is not None) == (expect[2] is not None)
        return
    with pytest.raises(expect[0], match=expect[1]):
        getattr(backend, method)(*args, **kwargs)


# --- HOSTED + G166 ----------------------------------------------------------------------------------------------
# (label, name, hosted?)
HOSTED_ROWS = [
    ("memory_save is hosted", "memory_save", True),
    ("memory_save_conversation is hosted", "memory_save_conversation", True),
    ("memory_save_document is not hosted (path-taking)", "memory_save_document", False),
] + [(f"{name} is not hosted", name, False) for name in GONE_NAMES]


@pytest.mark.parametrize(
    "label,name,hosted", HOSTED_ROWS, ids=[r[0] for r in HOSTED_ROWS]
)
def test_hosted_allowlist_rows(label, name, hosted):
    assert (name in HOSTED_TOOLS) is hosted


_G166_SNIPPET = """\
import asyncio
import json
import os
import smartmemory_mcp.tier as tier
import smartmemory_mcp.capabilities as capabilities
from smartmemory_mcp.backends.remote import RemoteBackend
from fastmcp import Client
from fastmcp.exceptions import ToolError

tier.get_api_key = lambda: os.environ.get("SMARTMEMORY_API_KEY", "")
capabilities.get_backend = lambda: RemoteBackend(api_key="sk_test_key_123")
from smartmemory_mcp.server import mcp

GONE = json.loads(os.environ["G166_NAMES"])


async def main():
    names = sorted(tool.name for tool in await mcp.list_tools())
    refused = {}
    async with Client(mcp) as client:
        for name in GONE:
            try:
                await client.call_tool(name, {})
                refused[name] = "CALL SUCCEEDED"
            except ToolError as exc:
                refused[name] = str(exc)
    print(json.dumps({"names": names, "refused": refused}))


asyncio.run(main())
"""


@pytest.fixture(scope="module")
def g166_listing():
    """One subprocess for the whole class: registration is module-level and tier-dependent, so it needs a clean process."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SMARTMEMORY_API_KEY", "SMARTMEMORY_MCP_FULL_TOOLS")
    }
    env.update(
        SMARTMEMORY_API_KEY="sk_test_key_123",
        SMARTMEMORY_MCP_FULL_TOOLS="true",
        G166_NAMES=json.dumps(list(GONE_NAMES)),
    )
    result = subprocess.run(
        [sys.executable, "-c", _G166_SNIPPET],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=MCP_ROOT,
    )
    assert result.returncode == 0, (
        f"stdout: {result.stdout}\nstderr: {result.stderr[-2000:]}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("old", GONE_NAMES)
def test_g166_old_tool_names_are_gone_with_no_alias(g166_listing, old):
    """G166 negative test: not listed, and calling it is an unknown-tool error (no alias, no shim)."""
    assert old not in g166_listing["names"]
    assert g166_listing["refused"][old] == f"Unknown tool: '{old}'"


def test_g166_the_three_save_tools_are_listed(g166_listing):
    names = set(g166_listing["names"])
    assert {"memory_save", "memory_save_document", "memory_save_conversation"} <= names
