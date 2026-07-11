"""Canonical response models for the unified MCP server.

MemoryResult is the normalized shape that both LocalBackend and RemoteBackend
produce for read-path operations (get, search, list). Tool code accesses fields
directly without isinstance/getattr guards.

See: docs/features/PLAT-MCP-MODELS-1/design.md
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


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
    derived_from: Optional[str]
    origin: Optional[str]
    reference: Optional[bool]
    entities: Optional[list]
    relations: Optional[list]
    drift_warnings: Optional[list]


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
        d = raw.to_dict()
    else:
        # Extract canonical + alias fields from object attributes
        d = {k: getattr(raw, k, None) for k in MemoryResult.__annotations__}
        # Resolve aliases that aren't in MemoryResult.__annotations__
        if not d.get("item_id"):
            d["item_id"] = getattr(raw, "id", None)
        if not d.get("created_at"):
            d["created_at"] = getattr(raw, "transaction_time", None)

    return MemoryResult(
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
        derived_from=d.get("derived_from"),
        origin=d.get("origin"),
        reference=d.get("reference"),
        entities=d.get("entities"),
        relations=d.get("relations"),
        drift_warnings=d.get("drift_warnings"),
    )


def normalize_items(raw_list: list) -> list[MemoryResult]:
    """Normalize a list of raw items into MemoryResult dicts."""
    return [normalize_item(item) for item in raw_list]
