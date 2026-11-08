"""Conflict review queue and supersession policy MCP tools (OUTREACH-MARIUS-HITL-SURFACE-1).

Remote: ``GET /memory/reasoning/reviews``, ``POST /memory/reasoning/reviews/{id}/settle`` and the WS-MGMT-16
settings knob ``review.supersession`` on ``/memory/workspaces/{id}/settings``. The workspace always comes from the
session; the settle body never names an actor (the service records the authenticated caller).
Local: the core facade on ``backend._mem``; a local settle is recorded as ``local:mcp``. The policy is a hosted
workspace setting, so the policy tools are remote-only.
Contract: smart-memory-docs ``docs/features/OUTREACH-MARIUS-HITL-SURFACE-1/review-queue-contract.json``.
"""

import json
import logging

from mcp.types import ToolAnnotations

from smartmemory_mcp.backends.path_guard import path_id
from .common import get_backend, graceful

logger = logging.getLogger(__name__)

LOCAL_ACTOR = "local:mcp"
_ACTIONS = ("keep_a", "keep_b", "keep_both")
_STATUSES = ("open", "resolved")
_POLICIES = ("retire", "propose", "inherit")
_POLICY_REMOTE_ONLY = (
    "The supersession policy is a hosted workspace setting; it needs the remote backend "
    "(smartmemory setup --mode remote)."
)
_LOCAL_UNAVAILABLE = "The review queue needs the local backend (smartmemory package) or the remote REST API."


def _error(result) -> str | None:
    if isinstance(result, dict) and "error" in result:
        status = result.get("status_code")
        return (
            f"Error ({status}): {result['error']}"
            if status
            else f"Error: {result['error']}"
        )
    return None


def _format_review(row: dict) -> str:
    conflict = row.get("conflict") or {}
    parts = [
        f"[{row.get('id')}] {row.get('status')} ({row.get('trigger') or 'conflict'})",
        f"  existing ({conflict.get('existing_item_id', '?')}): {conflict.get('existing_fact', '')}",
        f"  new: {conflict.get('new_fact', '')}",
    ]
    if row.get("status") == "resolved":
        parts.append(
            f"  settled: {row.get('resolution_action')} by {row.get('resolved_by')}"
            f" ({row.get('application_status') or 'applied'}): {row.get('resolution_reason')}"
        )
    return "\n".join(parts)


def _policy_view(workspace_id: str, settings: dict) -> dict:
    def knob(section: str):
        return ((settings.get(section) or {}).get("review") or {}).get("supersession")

    return {
        "workspace_id": settings.get("scope_id") or workspace_id,
        "policy": knob("effective"),
        "source": knob("source"),
        "stated": knob("stated"),
        "version": settings.get("version"),
    }


def register(mcp):
    """Register the review queue and supersession policy tools."""

    @mcp.tool(
        title="List conflict reviews",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def review_queue_list(
        status: str = "open", limit: int = 50, offset: int = 0
    ) -> str:
        """List the conflict reviews waiting for a human decision in the current workspace.

        Args:
            status: "open" (default) or "resolved".
            limit: 1..500 (default 50).
            offset: Rows to skip (default 0).
        """
        if status not in _STATUSES:
            return f"status must be one of {', '.join(_STATUSES)}"
        if not 1 <= limit <= 500 or offset < 0:
            return "limit must be 1..500 and offset >= 0"
        backend = get_backend()
        if backend.supports("request"):
            result = backend.request(
                "GET",
                "/memory/reasoning/reviews",
                params={"status": status, "limit": limit, "offset": offset},
            )
            if err := _error(result):
                return err
            rows = result.get("items", []) if isinstance(result, dict) else []
        else:
            mem = getattr(backend, "_mem", None)
            if mem is None:
                return _LOCAL_UNAVAILABLE
            rows = mem.list_conflict_reviews(status=status, limit=limit, offset=offset)
        if not rows:
            return f"No {status} conflict reviews."
        return f"{len(rows)} {status} conflict review(s):\n" + "\n".join(
            _format_review(r) for r in rows
        )

    @mcp.tool(
        title="Settle a conflict review",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def review_queue_settle(review_id: str, action: str, reason: str) -> str:
        """Settle one conflict review. The decision is recorded once; an identical retry returns the
        stored receipt, a different decision is refused.

        Args:
            review_id: The review id from review_queue_list.
            action: "keep_a" (keep the existing fact), "keep_b" (the new fact replaces it) or "keep_both".
            reason: Why, non-blank, up to 2000 characters. Recorded with the decision.
        """
        if action not in _ACTIONS:
            return f"action must be one of {', '.join(_ACTIONS)}"
        if not reason or not reason.strip() or len(reason) > 2000:
            return "reason must be non-blank and at most 2000 characters"
        backend = get_backend()
        if backend.supports("request"):
            result = backend.request(
                "POST",
                f"/memory/reasoning/reviews/{path_id(review_id, under='/memory/reasoning/reviews/')}/settle",
                json={"action": action, "reason": reason},
            )
            if err := _error(result):
                return err
            row = result if isinstance(result, dict) else {}
        else:
            mem = getattr(backend, "_mem", None)
            if mem is None:
                return _LOCAL_UNAVAILABLE
            try:
                row = mem.settle_conflict_review(
                    review_id, action=action, reason=reason, actor=LOCAL_ACTOR
                )
            except LookupError:
                return f"Conflict review {review_id} not found in this workspace"
            except (PermissionError, ValueError) as exc:
                return f"Refused: {exc}"
        return "Settled:\n" + _format_review(row)

    @mcp.tool(
        title="Read the supersession policy",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def supersession_policy_get() -> str:
        """Read the current workspace's supersession policy: "retire" lets a newer fact retire the
        older one at once, "propose" files a conflict review for a human instead."""
        backend = get_backend()
        if not backend.supports("request"):
            return _POLICY_REMOTE_ONLY
        workspace_id = backend.active_workspace_id
        result = backend.request(
            "GET",
            f"/memory/workspaces/{path_id(workspace_id, under='/memory/workspaces/')}/settings",
        )
        if err := _error(result):
            return err
        return json.dumps(
            _policy_view(workspace_id, result if isinstance(result, dict) else {})
        )

    @mcp.tool(
        title="Set the supersession policy",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def supersession_policy_set(policy: str) -> str:
        """Set the current workspace's supersession policy. Needs edit rights on the workspace;
        an API key or agent may only tighten it (set "propose").

        Args:
            policy: "retire", "propose", or "inherit" to clear the workspace override.
        """
        if policy not in _POLICIES:
            return f"policy must be one of {', '.join(_POLICIES)}"
        backend = get_backend()
        if not backend.supports("request"):
            return _POLICY_REMOTE_ONLY
        workspace_id = backend.active_workspace_id
        stated = None if policy == "inherit" else policy
        result = backend.request(
            "PATCH",
            f"/memory/workspaces/{path_id(workspace_id, under='/memory/workspaces/')}/settings",
            json={"stated": {"review": {"supersession": stated}}},
        )
        if err := _error(result):
            return err
        return json.dumps(
            _policy_view(workspace_id, result if isinstance(result, dict) else {})
        )
