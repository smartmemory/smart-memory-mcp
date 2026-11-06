"""Structured human procedures over scoped CRUD (SM-PROCEDURE-MCP-1).

The procedure engine is an optional core dependency, as with pattern tools.
These stdio tools are withheld from hosted mode pending its separate surface audit.
"""

from __future__ import annotations

from functools import wraps
import logging
from typing import Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .common import get_backend

logger = logging.getLogger(__name__)


class RemoteProcedureStore:
    """Use the authenticated backend's existing CRUD, preserving its workspace.

    Structured runbooks must use direct add. The generic remote add convenience
    omits use_pipeline=False, so send the existing REST body explicitly here.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self._cursors: dict[tuple[str, str], tuple[int, str | None]] = {}

    def add(self, content: str, memory_type: str, metadata: dict) -> str:
        from smartmemory_mcp.backends.receipts import read_wire_save

        result = self.backend.request(
            "POST",
            "/memory/add",
            json={
                "content": content,
                "memory_type": memory_type,
                "metadata": metadata,
                "use_pipeline": False,
                "embed": False,
            },
        )
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"Procedure write refused: {result['error']}")
        if not isinstance(result, dict) or result.get("status") == "held":
            raise RuntimeError(
                "Procedure write refused: service did not commit the item"
            )
        if "schema_version" in result:
            item_id = read_wire_save(result, op="procedure_add").value
        else:
            item_id = result.get("item_id") or result.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise RuntimeError(
                "Procedure write refused: service returned no committed item ID"
            )
        return item_id

    def get(self, item_id: str) -> dict | None:
        return self.backend.get(item_id)

    def update(self, item_id: str, *, metadata: dict) -> Any:
        result = self.backend.update(item_id, metadata=metadata)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError("Procedure update refused by service")
        return result

    def list_memories(
        self, *, limit: int, offset: int, metadata_key: str, metadata_value: str
    ) -> Any:
        from smartmemory_mcp.backends.models import normalize_items

        key = (metadata_key, metadata_value)
        if offset == 0:
            self._cursors[key] = (0, None)
        expected, cursor = self._cursors.get(key, (-1, None))
        if expected != offset:
            raise ValueError(
                "Procedure pages must be read sequentially from offset zero"
            )
        params = {
            "limit": limit,
            "metadata_key": metadata_key,
            "metadata_value": metadata_value,
        }
        if cursor is not None:
            params["cursor"] = cursor
        result = self.backend.request("GET", "/memory/list", params=params)
        if (
            not isinstance(result, dict)
            or result.get("error")
            or not isinstance(result.get("items"), list)
        ):
            raise RuntimeError(
                "Procedure list refused: service returned no complete page"
            )
        if result.get("has_more") and not result.get("next_cursor"):
            raise RuntimeError(
                "Procedure list refused: service omitted continuation cursor"
            )
        self._cursors[key] = (offset + len(result["items"]), result.get("next_cursor"))
        if not result.get("has_more"):
            self._cursors.pop(key, None)
        return {**result, "items": normalize_items(result["items"])}


def _manager():
    from smartmemory.procedures.human import ProcedureManager
    from smartmemory.procedures.store import MemoryProcedureStore
    from smartmemory_mcp.backends.local import LocalBackend

    backend = get_backend()  # Resolve authority anew on every call.
    store = (
        MemoryProcedureStore(backend._mem)
        if isinstance(backend, LocalBackend)
        else RemoteProcedureStore(backend)
    )
    return ProcedureManager(store)


def _checked(fn):
    @wraps(fn)
    def invoke(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "%s refused; procedure data or write unavailable: %s",
                fn.__name__,
                type(exc).__name__,
            )
            raise ToolError(str(exc)) from exc

    return invoke


def register(mcp):
    """Register all six tools. No procedure argument may override store identity."""
    read = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
    transition = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(annotations=write)
    @_checked
    def procedure_create(
        name: str,
        steps: list[str],
        expected_outcome: str,
        description: str = "",
        preconditions: list[str] | None = None,
        candidate: bool = False,
    ) -> dict:
        """Save a human runbook with ordered steps, preconditions and expected outcome."""
        return _manager().create(
            name,
            steps,
            expected_outcome,
            description=description,
            preconditions=preconditions,
            candidate=candidate,
        )

    @mcp.tool(annotations=read)
    @_checked
    def procedure_get(procedure_id: str) -> dict | None:
        """Get a human procedure and its latest outcomes in the current workspace."""
        return _manager().get(procedure_id)

    @mcp.tool(annotations=read)
    @_checked
    def procedure_list(
        task: str | None = None, status: str = "active", limit: int = 20
    ) -> list[dict]:
        """List human procedures, optionally rank active runbooks against task text."""
        return _manager().list(task=task, status=status, limit=limit)

    @mcp.tool(annotations=transition)
    @_checked
    def procedure_promote(procedure_id: str) -> dict:
        """Promote a saved human candidate to active. Dismissed procedures stay dismissed."""
        return _manager().promote(procedure_id)

    @mcp.tool(annotations=transition)
    @_checked
    def procedure_dismiss(procedure_id: str, reason: str = "") -> dict:
        """Dismiss a human procedure while preserving its body and outcome history."""
        return _manager().dismiss(procedure_id, reason)

    @mcp.tool(annotations=write)
    @_checked
    def procedure_record_outcome(
        procedure_id: str, success: bool, note: str = ""
    ) -> dict:
        """Record a completed run, reinforcing or weakening its evidence-based confidence."""
        return _manager().record_outcome(procedure_id, success, note)
