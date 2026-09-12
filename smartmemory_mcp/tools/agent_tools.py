"""Agent recall profile MCP tools (rewritten for unified MCP -- no MongoDB)."""

import json
import logging
from typing import Dict, Optional

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


_LOCAL_UNSUPPORTED = (
    "Recall profiles are not supported in local mode: nothing on the local search path "
    "reads them (`apply_recall_profile` is only called by the hosted service). Use remote "
    "mode (set SMARTMEMORY_API_URL + SMARTMEMORY_API_KEY) to set an agent recall profile."
)


def _rest(backend):
    """The REST escape hatch, or None in local mode.

    The recall profile lives in the service's agent record and is applied at search time
    by the service. Writing it anywhere else (this module used to store it as a procedural
    memory item) produces a profile that reads back correctly and is never applied.
    """
    return getattr(backend, "request", None)


def register(mcp):
    """Register agent recall profile tools with the MCP server."""

    @mcp.tool(
        title="Set recall profile",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def agent_set_recall_profile(
        agent_id: str,
        memory_type_weights: Optional[Dict[str, float]] = None,
    ) -> str:
        """Set an agent's recall profile for personality-aware search re-ranking."""
        backend = get_backend()

        if memory_type_weights:
            for k, v in memory_type_weights.items():
                if not isinstance(v, (int, float)) or v < 0:
                    return (
                        f"Error: weight for '{k}' must be non-negative number, got {v}"
                    )

        request = _rest(backend)
        if request is None:
            raise NotImplementedError(_LOCAL_UNSUPPORTED)

        profile = {"memory_type_weights": memory_type_weights or {}}
        result = request(
            "PUT",
            f"/memory/agents/{agent_id}/recall-profile",
            json={"recall_profile": profile},
        )
        if isinstance(result, dict) and result.get("error"):
            return f"Failed to set recall profile for {agent_id}: {result['error']}"

        if memory_type_weights:
            weight_str = ", ".join(f"{k}: {v}x" for k, v in memory_type_weights.items())
            return f"Recall profile set for {agent_id}: {weight_str}"
        return f"Recall profile cleared for {agent_id}."

    @mcp.tool(
        title="Get recall profile",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def agent_get_recall_profile(agent_id: str) -> str:
        """Get an agent's recall profile."""
        backend = get_backend()

        request = _rest(backend)
        if request is None:
            raise NotImplementedError(_LOCAL_UNSUPPORTED)

        result = request("GET", f"/memory/agents/{agent_id}/recall-profile")
        if isinstance(result, dict) and result.get("error"):
            return f"Failed to read recall profile for {agent_id}: {result['error']}"

        profile = (result or {}).get("recall_profile") or {}
        if not profile.get("memory_type_weights"):
            return f"Agent {agent_id} has no recall profile (default behavior)."
        return f"Recall profile for {agent_id}: {json.dumps(profile, indent=2)}"

    @mcp.tool(
        title="Get an agent evaluation",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def agent_evaluation_get(
        agent_id: str,
        dimension: str,
        domain: str,
    ) -> Optional[Dict]:
        """Get the current evaluation score for an agent on a given (dimension, domain) slot.

        CORE-AGENT-2 S03-T9. No tenant_id — matches agent_*_recall_profile sibling
        convention in the standalone MCP repo (Codex Round-2 F5 / OQ2-bis).

        Returns the current evaluation dict, or None when no evaluation has been
        written yet (cold-start).

        Returns raw dict | None per evaluation-contract.json §mcp_tools — NOT
        a formatted string. Cross-slice Codex review must-fix.

        dimension: one of decision_volume, supersession_rate, confidence_trajectory,
                   reinforcement_balance.
        domain: controlled-vocabulary domain string (e.g. 'python', 'kubernetes').
        """
        from smartmemory.agents.evaluation import get_evaluation as _get_evaluation

        backend = get_backend()
        sm = getattr(backend, "_mem", None)
        if sm is None:
            # Evaluations are read from the graph; passing the MCP backend wrapper
            # (which has no SmartMemory/_graph) made get_evaluation ALWAYS return
            # None (2026-06-02 bug hunt). Remote-mode client-side read is a follow-on.
            logger.debug(
                "agent_evaluation_get: no local SmartMemory (remote mode); returning None"
            )
            return None
        return _get_evaluation(sm, agent_id, dimension, domain)
