"""U4.32 (N-02.activate.outer-readers): LocalBackend reads a core save return as a receipt or a legacy shape.

Legacy returns (id string, held dict) are unchanged and import nothing. A receipt (object or wire dict) reads as
its subject id for ``add`` and in full through ``add_with_receipt``. A rejected or unreadable receipt is a typed
error with one WARNING and never an id. The MCP tool text for both arms is pinned (R15: both shapes parse).
No core producer returns a receipt yet, so a scripted ``_mem.add`` stands in for it.
"""

from __future__ import annotations

import builtins
import logging
import sys
from unittest.mock import MagicMock

import pytest

from smartmemory_mcp.backends import local
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import memory_tools

receipt_mod = pytest.importorskip("smartmemory.save.receipt")
save_return = pytest.importorskip("smartmemory_app.save_return")

LOGGER = "smartmemory_mcp.backends.local"
OURS = (LOGGER, "smartmemory_app.save_return")


def ours(caplog):
    """The save-return records only: core's own origin-policy WARNING on ``add`` is not this unit's."""
    return [r for r in caplog.records if r.name in OURS]


def backend(result):
    be = LocalBackend.__new__(LocalBackend)
    be._mem = MagicMock()
    be._mem.add.return_value = result
    return be


def receipt(**over):
    return receipt_mod.build_local_receipt(
        subject=receipt_mod.ReceiptSubject("item_save", item_id="test_item"),
        run_id="test_run",
        processing_stage="stored",
        execution_state="queued",
        outcome=None,
        terminal=False,
        result_availability="pending",
        revision=1,
        **over,
    )


def skipped_receipt():
    return receipt_mod.build_local_receipt(
        subject=receipt_mod.ReceiptSubject("item_save", item_id="test_item"),
        run_id="test_run",
        processing_stage="stored",
        execution_state="completed",
        outcome="skipped",
        terminal=True,
        result_availability="none",
        revision=1,
    )


def rejection(code="idempotency_conflict"):
    return receipt_mod.decode_receipt(
        {
            "schema_version": "1.1",
            "admission_error": {"code": code, "message": f"test {code}"},
        }
    )


HELD = {
    "status": "held",
    "item_id": None,
    "queued": False,
    "reason": "low_confidence",
    "reasons": ["r"],
}

LEGACY_ROWS = [
    ("id string", "test-id-1"),
    ("held dict", HELD),
    ("queued dict", {"item_id": "test-id-2", "queued": True, "run_id": "r1"}),
    ("none", None),
]


@pytest.mark.parametrize(
    "result", [row[1] for row in LEGACY_ROWS], ids=[row[0] for row in LEGACY_ROWS]
)
def test_legacy_returns_are_unchanged_and_import_nothing(result, monkeypatch, caplog):
    real = builtins.__import__

    def no_wrapper(name, *args, **kwargs):
        if name.startswith("smartmemory_app.save_return"):
            raise AssertionError(f"legacy return imported {name}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_wrapper)
    be = backend(result)
    assert be.add("test content") is result
    kept = be.add_with_receipt("test content")
    assert kept.value is result and kept.receipt is None and kept.summary is None
    assert ours(caplog) == []


RECEIPT_ROWS = [
    ("item received", receipt, "test_item (stored)"),
    (
        "G98 extraction skipped",
        skipped_receipt,
        "test_item (Saved, extraction skipped)",
    ),
]


@pytest.mark.parametrize(
    "make, summary",
    [row[1:] for row in RECEIPT_ROWS],
    ids=[row[0] for row in RECEIPT_ROWS],
)
def test_receipt_reads_as_id_and_keeps_the_receipt(make, summary):
    obj = make()
    for shape in (obj, obj.to_dict()):
        be = backend(shape)
        assert be.add("test content") == "test_item"
        kept = be.add_with_receipt("test content")
        assert (kept.value, kept.receipt, kept.summary) == (
            "test_item",
            obj.to_dict(),
            summary,
        )
        assert be.add("test content") != str(obj)


def test_rejected_receipt_is_typed_logged_and_never_an_id(caplog):
    for shape in (rejection(), rejection().to_dict()):
        caplog.clear()
        be = backend(shape)
        for call in (be.add, be.add_with_receipt):
            with pytest.raises(save_return.SaveRejected):
                call("test content")
        assert [
            r.getMessage() for r in ours(caplog) if r.levelno >= logging.WARNING
        ] == [
            "Save return add rejected at admission (idempotency_conflict); nothing was stored under a new id"
        ] * 2


def test_unreadable_receipt_is_refused(caplog):
    be = backend({**receipt().to_dict(), "schema_version": "2.0"})
    with pytest.raises(save_return.SaveReturnRefused):
        be.add("test content")
    assert [(r.levelno, r.getMessage()) for r in ours(caplog)] == [
        (
            logging.WARNING,
            "Save return add refused: receipt refused by the reader (unsupported_version); no id returned",
        )
    ]


def test_receipt_without_the_wrapper_reader_is_one_warning_and_a_typed_error(
    monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, "smartmemory_app.save_return", None)
    import smartmemory_app

    monkeypatch.delattr(smartmemory_app, "save_return", raising=False)
    be = backend(receipt())
    with pytest.raises(RuntimeError, match="cannot read it"):
        be.add("test content")
    assert [(r.levelno, r.name) for r in ours(caplog)] == [(logging.WARNING, LOGGER)]
    assert ours(caplog)[0].getMessage() == (
        "Save return add refused: smartmemory_app.save_return is not installed, so a save receipt "
        "cannot be read; no id returned"
    )


def test_receipt_arm_classifier_table():
    rows = [
        ("test-id", False),
        ({"item_id": "x"}, False),
        (HELD, False),
        (None, False),
        ({"schema_version": "1.1"}, True),
        (receipt(), True),
        (rejection(), True),
    ]
    assert [local._is_receipt_arm(value) for value, _ in rows] == [
        expected for _, expected in rows
    ]


# -- tool text, both arms (R15) -------------------------------------------------------------------------
class FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def memory_add(monkeypatch):
    def arm(result):
        monkeypatch.setattr(memory_tools, "get_backend", lambda: backend(result))
        mcp = FakeMCP()
        memory_tools.register_pro(mcp)
        return mcp.fns["memory_add"]

    return arm


def test_memory_add_tool_text_is_the_same_for_both_arms(memory_add):
    assert (
        memory_add("test-id-1")("test content")
        == "Memory added (direct). Item ID: test-id-1"
    )
    held = memory_add(HELD)("test content")
    assert held == "Memory held: low_confidence. Details: ['r']"
    for shape in (receipt(), receipt().to_dict()):
        assert (
            memory_add(shape)("test content")
            == "Memory added (direct). Item ID: test_item"
        )
