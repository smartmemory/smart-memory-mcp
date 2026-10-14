"""Canonical response models for the unified MCP server.

MemoryResult is the normalized shape that both LocalBackend and RemoteBackend
produce for read-path operations (get, search, list). Tool code accesses fields
directly without isinstance/getattr guards.

See: docs/features/PLAT-MCP-MODELS-1/design.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)


@dataclass
class CodeIngestResult:
    """Remote summary matching core IndexResult, with standalone client notices."""

    replaced: bool = False
    entities_created: int = 0
    edges_created: int = 0
    errors: list[str] = field(default_factory=list)
    files_parsed: int = 0
    notice: str = ""
    error_message: str = ""
    files_clean: int | str = 0
    files_partial: int | str = 0
    files_failed: int | str = 0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    acceptance: str = "refused"
    staging: str = "not_started"
    publication: str = "refused"
    g16_complete: bool = False
    call_sites: dict[str, int] | str = "unknown"
    resolved_call_edges: int | str = "unknown"


class MemoryResult(TypedDict, total=False):
    """Normalized memory item for tool consumption."""

    item_id: str  # Canonical — resolved from item_id or id
    content: str
    memory_type: str
    metadata: dict
    # NOTE: server-only identity (workspace_id/tenant_id/team_id/user_id/run_id) is
    # deliberately NOT a field here — it must never reach an MCP client. Portability
    # (export/import) works off the scoped core exporter / service routes, not this
    # normalized model, so it does not need per-item workspace_id.
    created_at: str  # Canonical — resolved from transaction_time or created_at
    valid_start_time: Optional[str]
    valid_end_time: Optional[str]
    transaction_time: Optional[str]
    score: Optional[float]
    confidence: Optional[float]
    stale: bool
    # /memory/by-metadata returns these (crud.py get_memory_by_metadata); dropping
    # them made a SUPERSEDED memory indistinguishable from an active one, so a
    # caller could act on knowledge the system already knows is obsolete.
    superseded: bool
    superseded_by: Optional[str]
    derived_from: Optional[str]
    origin: Optional[str]
    reference: Optional[bool]
    entities: Optional[list]
    relations: Optional[list]
    drift_warnings: Optional[list]
    # PLAT-AUDITABLE-MEMORY-1 gap #2 — "resolved" | "unresolved" | "no_chain",
    # set only on as_of_date searches. "unresolved" means present-day content
    # is standing in for a historical belief; an agent must not cite it as history.
    as_of_resolution: Optional[str]
    # U5.22: response-only extraction status ({extraction_state, processing_stage,
    # execution_state, outcome, revision}) core attaches on the visibility-gate-ON
    # arm. Present only when the source carries it; absent on the legacy arm.
    read_status: Optional[dict]


class MalformedReadStatusError(ValueError):
    """A result carries a ``read_status`` that is present but unreadable (U5.22, C1/C8)."""


def validated_carrier(status: Any, item_id: Any) -> Optional[dict]:
    """``status`` as a carrier copy: None when absent, a typed refusal (one WARNING) when present but unreadable.

    Absent (None) is the legacy gate-OFF shape and stays unchanged. A present carrier must be a dict holding a
    non-empty string ``extraction_state``; anything else is never read as absent.
    """
    if status is None:
        return None
    if not isinstance(status, dict):
        reason = f"a {type(status).__name__}, not a dict"
    elif not (
        isinstance(status.get("extraction_state"), str) and status["extraction_state"]
    ):
        reason = "a dict without a string extraction_state"
    else:
        return dict(status)
    logger.warning(
        "read_status carrier on %s is %s: refusing to normalize the row without its extraction status",
        item_id,
        reason,
    )
    raise MalformedReadStatusError(f"read_status on {item_id} is {reason}")


def normalize_item(raw: Any, default_type: str = "semantic") -> MemoryResult:
    """Normalize a dict or object into a MemoryResult.

    Handles three input shapes:
    - dict (from RemoteBackend HTTP responses or LocalBackend delegations)
    - object with to_dict() (from smartmemory core MemoryItem)
    - arbitrary object (fallback via getattr)

    Resolves field aliases: id -> item_id, transaction_time -> created_at.
    """
    if isinstance(raw, dict):
        d = raw
    elif hasattr(raw, "to_dict"):
        # U5.22 (C1/C8): core's MemoryItem.to_dict() copies its response-only carrier with dict(), so an unreadable
        # one would escape as a raw ValueError/TypeError. Check it on the object first: same typed refusal and one
        # WARNING as every other shape.
        validated_carrier(
            getattr(raw, "__dict__", {}).get("read_status"),
            getattr(raw, "item_id", None),
        )
        d = raw.to_dict()
    else:
        # Extract canonical + alias fields from object attributes
        d = {k: getattr(raw, k, None) for k in MemoryResult.__annotations__}
        # Resolve aliases that aren't in MemoryResult.__annotations__
        if not d.get("item_id"):
            d["item_id"] = getattr(raw, "id", None)
        if not d.get("created_at"):
            d["created_at"] = getattr(raw, "transaction_time", None)

    result = MemoryResult(
        item_id=d.get("item_id") or d.get("id", ""),
        content=d.get("content", ""),
        memory_type=d.get("memory_type", default_type),
        metadata=d.get("metadata") or {},
        created_at=d.get("transaction_time") or d.get("created_at", ""),
        valid_start_time=d.get("valid_start_time"),
        valid_end_time=d.get("valid_end_time"),
        transaction_time=d.get("transaction_time"),
        score=d.get("score"),
        confidence=d.get("confidence"),
        stale=d.get("stale", False),
        superseded=bool(d.get("superseded", False)),
        superseded_by=d.get("superseded_by"),
        derived_from=d.get("derived_from"),
        origin=d.get("origin"),
        reference=d.get("reference"),
        entities=d.get("entities"),
        relations=d.get("relations"),
        drift_warnings=d.get("drift_warnings"),
        as_of_resolution=d.get("as_of_resolution"),
    )
    status = validated_carrier(d.get("read_status"), result["item_id"])
    if status is not None:
        result["read_status"] = status
    return result


def partial_reasons(rows: Any) -> tuple[str, ...]:
    """Why core called the answer behind ``rows`` partial (U5.21 F1): the carrier's ``.partial``, else ``()``.

    Read by attribute, not by importing core's ``PartialRows``: a remote-only install has no core. Anything that is not
    a tuple of strings (a plain list, a mock) is complete-or-unmeasured. Call it on the backend call's own return: a
    slice, a copy or a tier filter makes a plain list again.
    """
    names = getattr(rows, "partial", ())
    if isinstance(names, tuple) and names and all(type(n) is str for n in names):
        return names
    return ()


def partial_note(names: tuple[str, ...]) -> str:
    """One sentence for a model-facing answer built from a partial one, ``""`` when the answer is complete."""
    if not names:
        return ""
    reasons = ", ".join(
        n[:64] for n in names
    )  # core's names are short constants: the bound is for any other carrier
    # The reasons are core's coverage names and the client's own (``fetch_bound``, a lost lookup), so none is "core's".
    return f"The answer was partial ({reasons}), so matches past what was examined may not be shown."


def none_found(complete_text: str, subject: str, names: tuple[str, ...]) -> str:
    """``complete_text`` ("No X found ...") for a complete answer. A partial answer with nothing visible is not absence:
    it says what was visible, that the answer was partial and why."""
    if not names:
        return complete_text
    return f"No visible {subject} in the part core examined. {partial_note(names)} This is not proof that none exist."


def normalize_items(raw_list: list) -> list[MemoryResult]:
    """Normalize a list of raw items into MemoryResult dicts.

    The call's completeness survives (U5.21): a ``PartialRows`` in comes out as the same carrier over the normalized
    rows, and any other list comes out as the plain list it always did.
    """
    rows = [normalize_item(item) for item in raw_list]
    names = partial_reasons(raw_list)
    if not names:
        return rows
    from smartmemory.search.results import carry_partial

    return carry_partial(rows, names)
