"""U4.42 (C-01.activate.save.local-adapters): the local MCP backend's receipt save and structured-ingest reader.

Real lite SQLite memory (``create_lite_memory`` under ``tmp_path``), core's real normalizer and receipt codec. The one
stand-in is the save manager (installed at the facade flip, U20.S2): ``RecordingManager`` is a real receiver.

Classes pinned, one explicit row table each (no stacked parametrize):

* the local save: the retry key is carried or minted at this seam and survives a re-submission, the producer namespace
  is ``mcp.local`` (G158), waits reach core unchanged, the facade off is core's refusal with no add/ingest fallback;
* capability negotiation: ``supports("save")`` is True for Local only, and no tool calls the new methods;
* ``ingest_structured``: every legacy return unchanged (the OFF arm), a receipt read as its subject id, the receipt kept
  by ``ingest_structured_with_receipt``, a rejected receipt a typed error with one WARNING, and the tool text for both;
* a wrapper without the caller seam is one typed refusal with one WARNING.
"""

from __future__ import annotations

import ast
import builtins
import logging
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smartmemory_mcp.backends.local import (
    LocalBackend,
    SaveReturn,
    SaveReturnUnavailable,
)
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from smartmemory_mcp.tools import save_tools

receipt_mod = pytest.importorskip("smartmemory.save.receipt")
save_caller = pytest.importorskip("smartmemory_app.save_caller")

from smartmemory.pipeline import profile_outcomes as outcomes  # noqa: E402
from smartmemory.refusal_logging import refusal_logged  # noqa: E402
from smartmemory.save.facade import SaveNotEnabled, install_save_manager  # noqa: E402
from smartmemory.save.request import IncompatibleWait, bind_save_receipt  # noqa: E402
from smartmemory.save.status import ProcessingStage  # noqa: E402
from smartmemory.tools.factory import create_lite_memory  # noqa: E402
from smartmemory_app import storage  # noqa: E402
from smartmemory_app.save_return import SaveRejected  # noqa: E402

KEY_ALPHABET = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class RecordingManager:
    """A real receiver: records the hand-off and answers with a real G52 receipt."""

    def __init__(self):
        self.calls = []

    def receive(self, request, authority):
        self.calls.append((request, authority))
        return bind_save_receipt(
            request,
            item_id="test_item_u442",
            run_id="test_run_u442",
            revision=1,
            arm=outcomes.PERSISTED_EXTRACTION_DEFERRED,
            stage=ProcessingStage.RECEIVED,
        )


@pytest.fixture
def memory(tmp_path, monkeypatch):
    mem = create_lite_memory(data_dir=str(tmp_path / "test_u442_mcp"))
    monkeypatch.setattr(storage, "_data_path", tmp_path)
    try:
        yield mem
    finally:
        mem.close()


@pytest.fixture
def manager(memory):
    installed = RecordingManager()
    install_save_manager(memory, installed)
    return installed


def local(mem):
    be = LocalBackend.__new__(LocalBackend)
    be._mem = mem
    return be


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


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


# --- the local save -----------------------------------------------------------------------------------------------


def test_save_mints_a_key_in_cores_alphabet_and_names_the_mcp_local_producer(
    memory, manager
):
    result = local(memory).save("test u442 content", context={"session_id": "test_s1"})
    assert isinstance(result, SaveReturn) and result.value == "test_item_u442"
    assert result.receipt is not None and result.summary
    ((normalized, _),) = manager.calls
    assert normalized.idempotency_key.startswith("mcp:") and KEY_ALPHABET.fullmatch(
        normalized.idempotency_key
    )
    assert normalized.context["producer_namespace"] == "mcp.local"
    assert set(normalized.scoped_identities) == {"session_id"}


def test_a_supplied_key_is_carried_and_a_resubmitted_call_sends_the_same_request(
    memory, manager
):
    be = local(memory)
    call = be.save_call("test u442 retry", idempotency_key="test-key-u442")
    assert (
        call.idempotency_key == "test-key-u442"
        and call.adapter_namespace == "mcp.local"
    )
    be.submit_save(call)
    be.submit_save(call)
    (first, _), (second, _) = manager.calls
    assert (first.idempotency_key, first.content) == (
        second.idempotency_key,
        second.content,
    )
    assert first.idempotency_key == "test-key-u442"


def test_two_calls_without_a_key_mint_two_keys():
    be = LocalBackend.__new__(LocalBackend)
    assert be.save_call("x").idempotency_key != be.save_call("x").idempotency_key


# (label, kwargs, check on the normalized SaveRequest). Every wait value and the extract axis appear.
SAVE_ROWS = [
    ("wait-omitted", {}, lambda r: r.wait is None and r.extract),
    ("wait-stored", {"wait": "stored"}, lambda r: r.wait == "stored"),
    ("wait-extracted", {"wait": "extracted"}, lambda r: r.wait == "extracted"),
    ("extract-off", {"extract": False}, lambda r: r.wait is None and not r.extract),
    ("typed", {"memory_type": "semantic"}, lambda r: r.memory_type == "semantic"),
]


@pytest.mark.parametrize(
    "label, kwargs, check", SAVE_ROWS, ids=[r[0] for r in SAVE_ROWS]
)
def test_save_passes_each_field_to_core_unjudged(
    label, kwargs, check, memory, manager, caplog
):
    with caplog.at_level(logging.WARNING):
        result = local(memory).save("test u442 row", **kwargs)
    ((normalized, _),) = manager.calls
    assert check(normalized), label
    assert result.value == "test_item_u442"
    assert warnings_of(caplog) == []


def test_data_saves_do_not_extract_by_default(memory, manager):
    local(memory).save(data={"k": "v"})
    ((normalized, _),) = manager.calls
    assert (normalized.data, normalized.content, normalized.extract) == (
        {"k": "v"},
        None,
        False,
    )


def test_an_unreachable_wait_is_cores_typed_refusal_and_the_manager_is_untouched(
    memory, manager, caplog
):
    with caplog.at_level(logging.WARNING), pytest.raises(IncompatibleWait):
        local(memory).save("test u442 g98", extract=False, wait="extracted")
    assert warnings_of(caplog) == [
        "save refused (IncompatibleWait): Wait target is incompatible with this status; nothing was admitted"
    ]
    assert manager.calls == []


def test_the_facade_off_is_cores_refusal_and_never_add_or_ingest(memory, caplog):
    seen = []
    for name in ("add", "ingest", "ingest_structured", "direct_store"):
        original = getattr(memory, name)
        setattr(
            memory,
            name,
            lambda *a, _n=name, _o=original, **k: seen.append(_n) or _o(*a, **k),
        )
    nodes_before = sorted(memory._graph.nodes.nodes())
    with caplog.at_level(logging.WARNING), pytest.raises(SaveNotEnabled):
        local(memory).save("test u442 off")
    assert warnings_of(caplog) == [
        "save refused: no save manager is installed (the common-receipt facade is not enabled); "
        "nothing was admitted and add/ingest were not used"
    ]
    assert seen == [] and sorted(memory._graph.nodes.nodes()) == nodes_before


def test_a_rejected_admission_is_save_rejected_with_one_warning(memory, caplog):
    wire = {
        "schema_version": "1.1",
        "admission_error": {"code": "idempotency_conflict", "message": "test conflict"},
    }

    class Rejecting:
        def receive(self, request, authority):
            return receipt_mod.decode_receipt(wire)

    install_save_manager(memory, Rejecting())
    with caplog.at_level(logging.WARNING), pytest.raises(SaveRejected) as caught:
        local(memory).save("test u442 reject")
    assert caught.value.code == "idempotency_conflict"
    assert warnings_of(caplog) == [
        "Save return save rejected at admission (idempotency_conflict); nothing was stored under a new id"
    ]


# --- capability negotiation ---------------------------------------------------------------------------------------

# (label, backend class, whether it supports the receipt save). Local alone has it; Remote and Hosted keep the hosted
# API behaviour unchanged and stay unsupported.
CAPABILITY_ROWS = [
    ("local", LocalBackend, True),
    ("remote", RemoteBackend, False),
    ("hosted", HostedRemoteBackend, False),
]


@pytest.mark.parametrize(
    "label, backend, expected", CAPABILITY_ROWS, ids=[r[0] for r in CAPABILITY_ROWS]
)
def test_the_receipt_save_is_a_local_only_capability(label, backend, expected):
    assert backend.supports("save") is expected
    assert backend.supports("save_call") is expected
    assert backend.supports("submit_save") is expected


def test_the_unsupported_set_is_unchanged():
    assert "save" not in LocalBackend.unsupported_capabilities
    assert RemoteBackend.unsupported_capabilities.isdisjoint(
        {"save", "save_call", "submit_save"}
    )


def test_no_tool_calls_the_receipt_save_yet():
    names = {"save", "save_call", "submit_save"}
    root = Path(save_tools.__file__).parent
    called = {
        (path.name, node.func.attr)
        for path in root.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "backend"
        and node.func.attr in names
    }
    assert called == set()


# --- wrapper without the caller seam ------------------------------------------------------------------------------

# (label, the module made unimportable, the call). Both import sites of the backend are covered.
UNAVAILABLE_ROWS = [
    ("no-caller-module", "smartmemory_app.save_caller", lambda be: be.save_call("x")),
    (
        "no-storage-module",
        "smartmemory_app.storage",
        lambda be: be.submit_save(object()),
    ),
]


@pytest.mark.parametrize(
    "label, module, call", UNAVAILABLE_ROWS, ids=[r[0] for r in UNAVAILABLE_ROWS]
)
def test_a_wrapper_without_the_seam_is_one_typed_refusal(
    label, module, call, monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, module, None)
    be = LocalBackend.__new__(LocalBackend)
    be._mem = MagicMock()
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(SaveReturnUnavailable) as caught,
    ):
        call(be)
    assert warnings_of(caplog) == [
        "Save refused: the installed smartmemory wrapper has no receipt save (smartmemory_app.save_caller and "
        "storage.save), so a receipt save cannot be made; nothing was saved and add/ingest were not used"
    ]
    assert refusal_logged(caught.value)
    be._mem.assert_not_called()


# --- ingest_structured --------------------------------------------------------------------------------------------

HELD = {
    "status": "held",
    "item_id": None,
    "queued": False,
    "reason": "low_confidence",
    "reasons": [],
}

# Every legacy return is the OFF arm: handed back as the same object, the wrapper's reader not imported.
LEGACY_ROWS = [
    ("id string", "test-id-1"),
    ("queued dict", {"item_id": "test-id-2", "queued": True}),
    ("held dict", HELD),
    ("none", None),
]


def structured(result):
    be = LocalBackend.__new__(LocalBackend)
    be._mem = MagicMock()
    be._mem.ingest_structured.return_value = result
    return be


@pytest.mark.parametrize("label, result", LEGACY_ROWS, ids=[r[0] for r in LEGACY_ROWS])
def test_ingest_structured_legacy_returns_are_unchanged_and_import_nothing(
    label, result, monkeypatch, caplog
):
    real = builtins.__import__

    def no_reader(name, *args, **kwargs):
        if name.startswith("smartmemory_app.save_return"):
            raise AssertionError(f"legacy return imported {name}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_reader)
    be = structured(result)
    with caplog.at_level(logging.WARNING):
        assert be.ingest_structured({"k": 1}, schema="test_schema") is result
        kept = be.ingest_structured_with_receipt({"k": 1}, schema_name="test_schema")
    assert kept.value is result and kept.receipt is None and kept.summary is None
    assert warnings_of(caplog) == []
    assert be._mem.ingest_structured.call_args_list[0].kwargs == {
        "schema": "test_schema"
    }


def test_a_receipt_the_wrapper_cannot_read_is_one_marked_refusal_and_never_an_id(
    monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, "smartmemory_app.save_return", None)
    be = structured(receipt().to_dict())
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(SaveReturnUnavailable) as caught,
    ):
        be.ingest_structured({"k": 1}, schema="test_schema")
    assert warnings_of(caplog) == [
        "Save return ingest_structured refused: smartmemory_app.save_return is not installed, so a save receipt "
        "cannot be read; no id returned"
    ]
    assert refusal_logged(caught.value)


def test_ingest_structured_reads_a_receipt_as_its_subject_id_and_keeps_it():
    obj = receipt()
    for shape in (obj, obj.to_dict()):
        be = structured(shape)
        assert be.ingest_structured({"k": 1}, schema="test_schema") == "test_item"
        kept = be.ingest_structured_with_receipt({"k": 1}, schema="test_schema")
        assert (kept.value, kept.receipt) == ("test_item", obj.to_dict())
        assert be.ingest_structured({"k": 1}, schema="test_schema") != str(obj)


def test_ingest_structured_never_turns_a_rejection_into_an_id(caplog):
    be = structured(
        receipt_mod.decode_receipt(
            {
                "schema_version": "1.1",
                "admission_error": {
                    "code": "retry_key_expired",
                    "message": "test expired",
                },
            }
        )
    )
    with caplog.at_level(logging.WARNING), pytest.raises(SaveRejected):
        be.ingest_structured({"k": 1}, schema="test_schema")
    assert warnings_of(caplog) == [
        "Save return ingest_structured rejected at admission (retry_key_expired); nothing was stored under a new id"
    ]


class FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


# (label, core return, the id the tool returns, its text)
STRUCTURED_TOOL_ROWS = [
    (
        "legacy id",
        "test-id-1",
        "test-id-1",
        "Structured item saved. Schema: test_schema. Item ID: test-id-1",
    ),
    ("receipt", receipt(), "test_item", "Structured item: test_item (stored)"),
    ("receipt wire", receipt().to_dict(), "test_item", "Structured item: test_item (stored)"),
]


@pytest.mark.parametrize(
    "label, result, expected, text",
    STRUCTURED_TOOL_ROWS,
    ids=[r[0] for r in STRUCTURED_TOOL_ROWS],
)
def test_the_structured_save_tool_reads_both_arms(
    label, result, expected, text, monkeypatch
):
    monkeypatch.setattr(save_tools, "get_backend", lambda: structured(result))
    mcp = FakeMCP()
    save_tools.register_free(mcp)
    out = mcp.fns["memory_save"](data={"k": 1}, memory_type="test_schema")
    assert out.structured_content["item_id"] == expected
    assert out.content[0].text == text


# --- fix round 1 (F3): every new failure boundary is a typed, marked, single-WARNING refusal ---------------------------
# Bug class: C1 failure path without a typed/logged/marked refusal; C8 raw failure escape. One row per site of the
# census (u-U4.42/census.py). Each asserts the exact full WARNING list (logger, level, message), the typed error and
# the propagated core mark.

BACKEND_LOGGER = "smartmemory_mcp.backends.local"
UNAVAILABLE_WARNING = (
    "Save refused: the installed smartmemory wrapper has no receipt save (smartmemory_app.save_caller and "
    "storage.save), so a receipt save cannot be made; nothing was saved and add/ingest were not used"
)
UNREADABLE_WARNING = (
    "Save return ingest_structured refused: smartmemory_app.save_return is not installed, so a save receipt "
    "cannot be read; no id returned"
)


def _partial_module(monkeypatch, name):
    import types

    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def _records(caplog):
    return [
        (r.name, r.levelno, r.getMessage())
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ]


def _wrapper_without_the_export(monkeypatch, tmp_path, mem):
    _partial_module(monkeypatch, "smartmemory_app.save_caller")
    return (
        lambda: local(mem).save_call("x"),
        SaveReturnUnavailable,
        [(BACKEND_LOGGER, logging.WARNING, UNAVAILABLE_WARNING)],
        True,
    )


def _wrapper_storage_without_save(monkeypatch, tmp_path, mem):
    _partial_module(monkeypatch, "smartmemory_app.storage")
    return (
        lambda: local(mem).submit_save(object()),
        SaveReturnUnavailable,
        [(BACKEND_LOGGER, logging.WARNING, UNAVAILABLE_WARNING)],
        True,
    )


def _missing_export_in_a_core_without_the_mark(monkeypatch, tmp_path, mem):
    monkeypatch.setitem(sys.modules, "smartmemory.refusal_logging", None)
    _partial_module(monkeypatch, "smartmemory_app.save_caller")
    return (
        lambda: local(mem).save_call("x"),
        SaveReturnUnavailable,
        [(BACKEND_LOGGER, logging.WARNING, UNAVAILABLE_WARNING)],
        False,
    )


def _unreadable_receipt_in_a_core_without_the_mark(monkeypatch, tmp_path, mem):
    monkeypatch.setitem(sys.modules, "smartmemory.refusal_logging", None)
    monkeypatch.setitem(sys.modules, "smartmemory_app.save_return", None)
    return (
        lambda: structured(receipt().to_dict()).ingest_structured(
            {"k": 1}, schema="test_schema"
        ),
        SaveReturnUnavailable,
        [(BACKEND_LOGGER, logging.WARNING, UNREADABLE_WARNING)],
        False,
    )


def _write_lock_fails(monkeypatch, tmp_path, mem):
    def boom(path):
        raise OSError("test lock failure")

    monkeypatch.setattr(storage, "_get_lock_file", boom)
    return (
        lambda: local(mem).save("x"),
        save_caller.SaveCallRefused,
        [
            (
                "smartmemory_app.save_caller",
                logging.WARNING,
                "Save call refused (write_lock_unavailable): the local write lock could not be taken "
                "(OSError: test lock failure); nothing was saved and ingest was not used",
            )
        ],
        True,
    )


def _structured_receipt_is_not_a_save(monkeypatch, tmp_path, mem):
    stage_job = receipt_mod.build_local_receipt(
        subject=receipt_mod.ReceiptSubject(
            "stage_job", occurrence_id="test_occ", stage_id="test_stage"
        ),
        run_id="test_run",
        processing_stage=None,
        execution_state="queued",
        outcome=None,
        terminal=False,
        result_availability="pending",
    )
    from smartmemory_app.save_return import SaveReturnRefused

    return (
        lambda: structured(stage_job).ingest_structured_with_receipt(
            {"k": 1}, schema="test_schema"
        ),
        (SaveReturnRefused),
        [
            (
                "smartmemory_app.save_return",
                logging.WARNING,
                "Save return ingest_structured refused: receipt subject stage_job is not a save; no id returned",
            )
        ],
        True,
    )


def _structured_rejection_is_marked(monkeypatch, tmp_path, mem):
    rejected = receipt_mod.decode_receipt(
        {
            "schema_version": "1.1",
            "admission_error": {
                "code": "idempotency_conflict",
                "message": "test conflict",
            },
        }
    )
    return (
        lambda: structured(rejected).ingest_structured({"k": 1}, schema="test_schema"),
        SaveRejected,
        [
            (
                "smartmemory_app.save_return",
                logging.WARNING,
                "Save return ingest_structured rejected at admission (idempotency_conflict); "
                "nothing was stored under a new id",
            )
        ],
        True,
    )


# (label, row builder). A row builder returns (act, error type, the exact WARNING list, marked).
BOUNDARY_ROWS = [
    ("wrapper save_caller lacks the export", _wrapper_without_the_export),
    ("wrapper storage lacks save", _wrapper_storage_without_save),
    (
        "missing export in a core without the mark",
        _missing_export_in_a_core_without_the_mark,
    ),
    (
        "unreadable receipt in a core without the mark",
        _unreadable_receipt_in_a_core_without_the_mark,
    ),
    ("write lock cannot be taken", _write_lock_fails),
    ("structured receipt that is not a save", _structured_receipt_is_not_a_save),
    ("structured rejection", _structured_rejection_is_marked),
]


@pytest.mark.parametrize(
    "label, build", BOUNDARY_ROWS, ids=[r[0] for r in BOUNDARY_ROWS]
)
def test_every_new_failure_boundary_is_one_typed_marked_refusal(
    label, build, memory, monkeypatch, tmp_path, caplog
):
    act, error, warnings, marked = build(monkeypatch, tmp_path, memory)
    with caplog.at_level(logging.WARNING), pytest.raises(error) as caught:
        act()
    assert _records(caplog) == warnings, label
    assert refusal_logged(caught.value) is marked, label
