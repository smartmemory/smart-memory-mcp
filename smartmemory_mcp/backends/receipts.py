"""Read a save receipt off a service response (U4.42-FU-TOOLS; contract ``SM2-SAVE-1/save-receipt-contract.json``).

The service's 2.0 receipt route is not live yet, so this reader is coded against the contract and a fake. A response
that is not a mapping carrying ``schema_version`` is the legacy arm (an id, a held dict, a pipeline result) and passes
through untouched. A receipt is read by the contract's reader rules: any ``1.x`` minor is accepted and unknown fields
stay opaque, an unsupported major is a typed refusal that keeps the handle, and an ``admission_error`` receipt means the
save was rejected (no run exists). A receipt is never turned into an id it does not carry.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .local import SaveReturn

log = logging.getLogger(__name__)

#: The subject field that carries the saved object's id, by subject kind.
_SUBJECT_ID_FIELDS = ("item_id", "document_id", "generation_id")

#: ``execution_state`` / ``outcome`` values that mean the save ended without being kept or in error.
_FAILED = frozenset({"failed", "dead"})


class WireSaveRefused(RuntimeError):
    """A save receipt this reader will not turn into a result. ``code`` is the contract code, ``handle`` the run handle."""

    def __init__(self, code: str, message: str, handle: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.handle = handle


class WireSaveRejected(WireSaveRefused):
    """The service rejected the save at admission (an ``admission_error`` receipt): nothing was saved, no run exists."""


def _refuse(
    exc: WireSaveRefused, op: str
) -> WireSaveRefused:  # one WARNING per refusal, naming what was lost
    log.warning("Save return %s refused (%s): %s", op, exc.code, exc)
    return exc


def _handle(wire: Mapping[str, Any]) -> str | None:
    handle = wire.get("run_id") or wire.get("status_url")
    return str(handle) if handle else None


def _major(version: Any) -> int | None:
    head = str(version).split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _summary(subject_id: str, wire: Mapping[str, Any]) -> str:
    """``"<id> (<badge>)"``. An unknown stage, state or outcome is never read as a success."""
    state = wire.get("execution_state")
    outcome = wire.get("outcome")
    stage = wire.get("processing_stage")
    if wire.get("terminal"):
        if outcome in _FAILED or state in _FAILED:
            badge = "Failed"
        elif outcome == "skipped":
            badge = "Saved, extraction skipped"
        elif outcome in ("completed", "no_op") and stage == "extracted":
            badge = "Saved"
        elif outcome == "partial":
            badge = "Saved, partly processed"
        else:
            badge = "Saved, processing"
    elif state in _FAILED:
        badge = "Failed"
    elif stage == "received" or stage is None:
        badge = "Saving"
    else:
        badge = "Saved, processing"
    return f"{subject_id} ({badge})"


def read_wire_save(result: Any, *, op: str) -> SaveReturn:
    """Receipt-or-legacy view of one service save response (see the module docstring)."""
    if not isinstance(result, Mapping) or "schema_version" not in result:
        return SaveReturn(result)
    version = result["schema_version"]
    if _major(version) != 1:
        raise _refuse(
            WireSaveRefused(
                "unsupported_version",
                f"Save receipt schema_version {version!r} is not supported (this client reads 1.x); "
                f"nothing was inferred. Handle: {_handle(result) or 'none'}",
                _handle(result),
            ),
            op,
        )
    error = result.get("admission_error")
    if error is not None:
        detail = error if isinstance(error, Mapping) else {}
        raise _refuse(
            WireSaveRejected(
                str(detail.get("code") or "rejected"),
                str(detail.get("message") or "The save was rejected."),
            ),
            op,
        )
    subject = result.get("subject")
    subject_id = None
    if isinstance(subject, Mapping):
        subject_id = next(
            (subject[f] for f in _SUBJECT_ID_FIELDS if subject.get(f)), None
        )
    if not subject_id:
        raise _refuse(
            WireSaveRefused(
                "no_subject_id",
                f"Save receipt carries no subject id; no id returned. Handle: {_handle(result) or 'none'}",
                _handle(result),
            ),
            op,
        )
    return SaveReturn(
        str(subject_id), dict(result), _summary(str(subject_id), result)
    )
