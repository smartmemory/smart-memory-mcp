"""Zero-schema peer chat MCP tool (CORE-ZERO-SCHEMA-1 Phase 1)."""

import logging
from typing import Optional

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)

REASONING_LEVELS = ("minimal", "low", "medium", "high", "max")

# CORE-ZERO-SCHEMA-1 Phase 1: no `/memory/peer/*` service route exists yet, so the
# remote backend has nothing to proxy to. Same deferral shape as code_tools' provenance
# tools; the hosted surface lands with the Phase 4 rollout.
_PARKED_MSG = (
    "peer_chat is a local capability; the hosted REST surface is not built yet "
    "(CORE-ZERO-SCHEMA-1 Phase 1). Run this against a local backend."
)


def register(mcp):
    """Register zero-schema peer tools with the MCP server."""

    # The contract's "llm_unavailable raises" clause is satisfied at the core layer
    # (`peer_chat` raises RuntimeError rather than fabricate an answer); @graceful then
    # surfaces that as an explicit error payload here, because an MCP server must not
    # crash its transport on a tool failure. An explicit error return is not a silent
    # degradation — no answer is ever invented.
    @mcp.tool(
        title="Ask a peer about their memory",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    @graceful
    def peer_chat(
        peer_id: str,
        query: str,
        reasoning_level: str = "medium",
        session_id: Optional[str] = None,
        top_k: int = 10,
    ) -> dict:
        """Ask a question about a peer and get an answer synthesized from their memory.

        The zero-schema front door: no schema, no memory type, no ontology. Gathers the
        peer's expertise records, their recent messages (optionally scoped to one
        session), and one graph hop out of the top hits, then synthesizes a single
        answer over that context.

        Returns ``{answer, peer_id, reasoning_level, model, sources}`` matching
        ``peer-chat-contract.json``; ``sources`` entries carry ``item_id``,
        ``memory_type``, ``content_preview`` and ``channel``.

        Local-backend only in Phase 1: the hosted REST route is not built yet.
        """
        if not peer_id:
            return {"error": "`peer_id` is required."}
        if not query:
            return {"error": "`query` is required."}
        if reasoning_level not in REASONING_LEVELS:
            return {
                "error": f"`reasoning_level` must be one of {', '.join(REASONING_LEVELS)}."
            }

        backend = get_backend()
        if not backend.supports("peer_chat"):
            return {"error": _PARKED_MSG}

        return backend.peer_chat(
            peer_id=peer_id,
            query=query,
            reasoning_level=reasoning_level,
            session_id=session_id,
            top_k=top_k,
        )
