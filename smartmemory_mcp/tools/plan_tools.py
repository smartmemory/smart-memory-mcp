"""Plan management and failure journal MCP tools."""

import json
import logging
from typing import Any, Dict, List, Optional

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register plan and failure tools with the MCP server."""

    @mcp.tool()
    @graceful
    def memory_plan_create(
        title: str,
        tasks: Optional[List[Dict[str, Any]]] = None,
        context: str = "",
        created_by: str = "",
    ) -> str:
        """Create a plan with optional task nodes."""
        from smartmemory.plans.manager import PlanManager

        backend = get_backend()
        manager = PlanManager(backend)
        result = manager.create(title, tasks or [], context, created_by)
        return f"Plan created. plan_id: {result['plan_id']}, tasks: {len(result['task_ids'])}"

    @mcp.tool()
    @graceful
    def memory_plan_get(
        plan_id: str,
    ) -> str:
        """Get a plan with all its tasks."""
        from smartmemory.plans.manager import PlanManager

        backend = get_backend()
        manager = PlanManager(backend)
        result = manager.get(plan_id)
        if result is None:
            return f"Plan {plan_id} not found."
        return json.dumps(result, indent=2, default=str)

    @mcp.tool()
    @graceful
    def memory_plan_active() -> str:
        """Get all active plans."""
        from smartmemory.plans.manager import PlanManager

        backend = get_backend()
        manager = PlanManager(backend)
        plans = manager.get_active()
        if not plans:
            return "No active plans."
        lines = [f"Active plans ({len(plans)}):"]
        for p in plans:
            lines.append(
                f"  {p['plan_id']}: {p['content']} ({p['completed_tasks']}/{p['total_tasks']} tasks)"
            )
        return "\n".join(lines)

    @mcp.tool()
    @graceful
    def memory_plan_update_task(
        plan_id: str,
        task_id: str,
        status: str,
    ) -> str:
        """Update a task's status within a plan (pending, in_progress, complete, blocked)."""
        from smartmemory.plans.manager import PlanManager

        backend = get_backend()
        manager = PlanManager(backend)
        manager.update_task(plan_id, task_id, status)
        return f"Task {task_id} updated to '{status}' in plan {plan_id}."

    @mcp.tool()
    @graceful
    def memory_log_failure(
        error_type: str,
        content: str,
        context: str,
        plan_id: Optional[str] = None,
        task_id: Optional[str] = None,
        attempted_fix: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> str:
        """Log an agent failure for cross-session error recovery."""
        from smartmemory.plans.failure_journal import FailureJournal

        backend = get_backend()
        journal = FailureJournal(backend)
        item_id = journal.log(
            error_type=error_type,
            content=content,
            context=context,
            plan_id=plan_id,
            task_id=task_id,
            attempted_fix=attempted_fix,
            resolution=resolution,
        )
        return f"Failure logged. Item ID: {item_id}"

    @mcp.tool()
    @graceful
    def memory_check_failure(
        error_type: str,
        context: str,
        top_k: int = 3,
    ) -> str:
        """Check for matching past failures before retrying."""
        from smartmemory.plans.failure_journal import FailureJournal

        backend = get_backend()
        journal = FailureJournal(backend)
        matches = journal.check_before_retry(error_type, context, top_k)
        if not matches:
            return f"No prior failures matching {error_type} in this context."
        lines = [f"Found {len(matches)} prior failure(s):"]
        for m in matches:
            lines.append(f"  [{m['error_type']}] {m.get('content', '')[:80]}")
            if m.get("resolution"):
                lines.append(f"    Resolution: {m['resolution']}")
        return "\n".join(lines)
