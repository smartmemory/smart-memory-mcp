"""Conflict review queue and supersession policy MCP tools (OUTREACH-MARIUS-HITL-SURFACE-1).

The two review tools are also on the hosted connector (part 4): there the session's backend carries the signed-in
workspace and token, so the list and the settle are bound to that workspace and the service records the caller.

Remote: ``GET /memory/reasoning/reviews``, ``POST /memory/reasoning/reviews/{id}/settle`` and the WS-MGMT-16
settings knob ``review.supersession`` on ``/memory/workspaces/{id}/settings``. The workspace always comes from the
session; the settle body never names an actor (the service records the authenticated caller).
Local: the core facade on ``backend._mem``; a local settle is recorded as ``local:mcp``. The policy tools read and set
the Lite store's own settings row (``smartmemory.lite_settings``, reached through the store's scope provider): the local
tool is not a person, so it may only tighten (``propose``, ``person_only``), as an agent may on hosted; a person loosens
with the ``smartmemory --data-dir <dir> review-settings`` command.

Who may approve (part 7, 2.0.2): ``review.approval`` defaults to ``person_only``. Neither the hosted connector's
assistant (recorded ``connector:<user id>``) nor the local tool (``local:mcp``) is a person, so both are refused unless
a person set the workspace (or the store) to ``anyone``. A refusal's sentence is shown as the service or core wrote it.
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
_APPROVALS = ("anyone", "person_only", "inherit")
# The tight value of each knob: the only one the local tool (not a person) may set.
_TIGHT = {"supersession": "propose", "approval": "person_only"}
_CLI_FLAG = {
    "supersession": ("--hold", {"propose": "on", "retire": "off", None: "unset"}),
    "approval": (
        "--approval",
        {"anyone": "anyone", "person_only": "person_only", None: "unset"},
    ),
}
_DEFAULTS = {"supersession": "retire", "approval": "person_only"}
_LOCAL_SETTINGS_UNAVAILABLE = (
    "This local store has no settings row (it is not a Lite store opened by the smartmemory package); "
    "the setting cannot be read or changed here."
)
_LOCAL_UNAVAILABLE = "The review queue needs the local backend (smartmemory package) or the remote REST API."


def _detail(result: dict) -> str:
    """The service's own sentence (``detail`` or ``detail.message``), as is; else the raw error text."""
    body = result.get("body")
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail")
    return detail if isinstance(detail, str) and detail.strip() else result["error"]


def _error(result) -> str | None:
    if isinstance(result, dict) and "error" in result:
        status = result.get("status_code")
        return (
            f"Error ({status}): {_detail(result)}"
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


def _policy_view(
    workspace_id: str, settings: dict, knob_name: str = "supersession"
) -> dict:
    def knob(section: str):
        return ((settings.get(section) or {}).get("review") or {}).get(knob_name)

    return {
        "workspace_id": settings.get("scope_id") or workspace_id,
        "policy": knob("effective"),
        "source": knob("source"),
        "stated": knob("stated"),
        "version": settings.get("version"),
    }


def _lite_settings(backend):
    """The Lite store's settings row behind the local backend, or None (no core, or not a Lite store)."""
    mem = getattr(backend, "_mem", None)
    provider = getattr(mem, "scope_provider", None) if mem is not None else None
    settings = getattr(provider, "lite_settings", None)
    return (
        settings
        if settings is not None and callable(getattr(settings, "read", None))
        else None
    )


def _local_view(settings, knob_name: str) -> dict:
    stated = settings.read()[f"review.{knob_name}"]
    return {
        "workspace_id": "local",
        "policy": stated or _DEFAULTS[knob_name],
        "source": "store" if stated else "default",
        "stated": stated,
        "version": None,
    }


def _local_set(backend, knob_name: str, value) -> str:
    settings = _lite_settings(backend)
    if settings is None:
        return _LOCAL_SETTINGS_UNAVAILABLE
    if value != _TIGHT[knob_name]:
        flag, values = _CLI_FLAG[knob_name]
        data_dir = settings.db_path.parent
        return (
            f"Refused: the local connector is not a person, so it may only tighten review.{knob_name} "
            f'(set "{_TIGHT[knob_name]}"). A person changes it with: '
            f"smartmemory --data-dir {data_dir} review-settings {flag} {values[value]}"
        )
    settings.set(updated_by=LOCAL_ACTOR, **{knob_name: value})
    return json.dumps(_local_view(settings, knob_name))


def _remote_get(backend, knob_name: str) -> str:
    workspace_id = backend.active_workspace_id
    result = backend.request(
        "GET",
        f"/memory/workspaces/{path_id(workspace_id, under='/memory/workspaces/')}/settings",
    )
    if err := _error(result):
        return err
    return json.dumps(
        _policy_view(
            workspace_id, result if isinstance(result, dict) else {}, knob_name
        )
    )


def _remote_set(backend, knob_name: str, value) -> str:
    workspace_id = backend.active_workspace_id
    result = backend.request(
        "PATCH",
        f"/memory/workspaces/{path_id(workspace_id, under='/memory/workspaces/')}/settings",
        json={"stated": {"review": {knob_name: value}}},
    )
    if err := _error(result):
        return err
    return json.dumps(
        _policy_view(
            workspace_id, result if isinstance(result, dict) else {}, knob_name
        )
    )


def register(mcp):
    """Register the review queue, supersession policy and review approval tools."""

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
        """List what is waiting for a person in the current workspace: conflicting facts, and
        replacements of a decision (rule) that the workspace holds for a person.

        For a held replacement, "existing" is the rule still in force and "new" is the waiting
        rule. Show both to the user. A person settles it; do not settle one yourself.

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
            total = result.get("total") if isinstance(result, dict) else None
        else:
            mem = getattr(backend, "_mem", None)
            if mem is None:
                return _LOCAL_UNAVAILABLE
            rows = mem.list_conflict_reviews(status=status, limit=limit, offset=offset)
            count = getattr(mem, "count_conflict_reviews", None)
            total = count(status=status) if callable(count) else None
        if not isinstance(total, int):
            total = None
        if not rows:
            if total:
                return f"No {status} conflict reviews on this page ({total} in total)."
            return f"No {status} conflict reviews."
        shown = f"{len(rows)} of {total}" if total is not None else f"{len(rows)}"
        return f"{shown} {status} conflict review(s):\n" + "\n".join(
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
        """Record a decision on one review. By default only a person approves: unless a person set
        the workspace (or the local store) to let anyone approve, this call is refused, because
        an assistant is not a person, and the review stays open. Then tell the user it waits for
        them: a person approves in the SmartMemory app (hosted) or with a call that names them
        (local). Where anyone may approve, the decision is recorded once, as connector:<user>
        (hosted) or local:mcp (local); an identical retry returns the stored receipt, a different
        answer is refused. Call it only to pass on what the user has just told you to decide;
        never to settle a change you filed yourself.

        For a held decision replacement: "keep_b" approves (the new rule is in force and the old
        one is listed as superseded), "keep_a" rejects (the old rule stays, nothing new is saved).

        Args:
            review_id: The review id from review_queue_list or decision_supersede.
            action: "keep_a" (keep the existing fact or rule), "keep_b" (the new one replaces it) or "keep_both".
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
        older one at once, "propose" files a conflict review for a human instead. Local: the
        store's own setting."""
        backend = get_backend()
        if not backend.supports("request"):
            settings = _lite_settings(backend)
            if settings is None:
                return _LOCAL_SETTINGS_UNAVAILABLE
            return json.dumps(_local_view(settings, "supersession"))
        return _remote_get(backend, "supersession")

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
        an API key, an agent or this connector may only tighten it (set "propose"). Local: the
        store's own setting, tighten only; a person loosens it with the review-settings command.

        Args:
            policy: "retire", "propose", or "inherit" to clear the workspace override.
        """
        if policy not in _POLICIES:
            return f"policy must be one of {', '.join(_POLICIES)}"
        backend = get_backend()
        stated = None if policy == "inherit" else policy
        if not backend.supports("request"):
            return _local_set(backend, "supersession", stated)
        return _remote_set(backend, "supersession", stated)

    @mcp.tool(
        title="Read who may approve reviews",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def review_approval_get() -> str:
        """Read who may approve (settle) reviews in the current workspace: "person_only" (the
        default: only a signed-in person, never an assistant or an API key) or "anyone" (any
        caller with write access). Local: the store's own setting."""
        backend = get_backend()
        if not backend.supports("request"):
            settings = _lite_settings(backend)
            if settings is None:
                return _LOCAL_SETTINGS_UNAVAILABLE
            return json.dumps(_local_view(settings, "approval"))
        return _remote_get(backend, "approval")

    @mcp.tool(
        title="Set who may approve reviews",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def review_approval_set(approval: str) -> str:
        """Set who may approve reviews in the current workspace. An assistant may only tighten it
        (set "person_only"); only a person may set "anyone" or clear it, so a loosening call from
        here is refused. Local: the store's own setting; a person loosens it with the
        review-settings command the refusal names.

        Args:
            approval: "person_only", "anyone", or "inherit" to clear the workspace override.
        """
        if approval not in _APPROVALS:
            return f"approval must be one of {', '.join(_APPROVALS)}"
        backend = get_backend()
        stated = None if approval == "inherit" else approval
        if not backend.supports("request"):
            return _local_set(backend, "approval", stated)
        return _remote_set(backend, "approval", stated)
