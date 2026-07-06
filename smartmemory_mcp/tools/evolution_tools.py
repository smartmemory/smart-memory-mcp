"""Evolution, clustering, and synthesis MCP tools."""

import logging

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register evolution tools with the MCP server."""

    @mcp.tool()
    @graceful
    def evolution_trigger() -> str:
        """Trigger a memory evolution cycle — runs clustering, deduplication, and consolidation."""
        backend = get_backend()
        backend.run_evolution_cycle()
        return "Memory evolution cycle completed successfully."

    @mcp.tool()
    @graceful
    def evolution_dream() -> str:
        """**Deprecated:** CORE-MEMORY-DYNAMICS-1 M1b moved routing to at-ingest
        via the ConsolidationRouter.  This tool is a no-op kept for caller
        compatibility."""
        logger.warning(
            "evolution_dream is deprecated (CORE-MEMORY-DYNAMICS-1 M1b) — no-op."
        )
        return (
            "Dream phase is deprecated. Routing happens at ingest via the "
            "ConsolidationRouter (CORE-MEMORY-DYNAMICS-1). Nothing to do."
        )

    @mcp.tool()
    @graceful
    def evolution_status() -> str:
        """Get status of memory evolution processes."""
        backend = get_backend()

        try:
            # CORE-MEMORY-DYNAMICS-1 M1b: "working" memory_type renamed to "pending".
            pending_items = backend.search("*", memory_type="pending", top_k=100) or []
            pending_count = len(pending_items)
        except Exception:
            pending_count = 0

        status = "ready" if pending_count >= 1 else "idle"
        return (
            f"Evolution status: {status}\n"
            f"Pending memory items (formerly 'working'): {pending_count}"
        )

    @mcp.tool()
    @graceful
    def evolution_synthesize_opinions() -> str:
        """Run opinion synthesis — detect patterns in episodic memories and form opinions."""
        backend = get_backend()
        backend.run_evolver("opinion_synthesis", log=logger)
        return "Opinion synthesis completed."

    @mcp.tool()
    @graceful
    def evolution_synthesize_observations() -> str:
        """Run observation synthesis — create entity summaries from scattered facts."""
        backend = get_backend()
        backend.run_evolver("observation_synthesis", log=logger)
        return "Observation synthesis completed."

    @mcp.tool()
    @graceful
    def evolution_reinforce_opinions() -> str:
        """Run opinion reinforcement — update confidence scores based on new evidence."""
        backend = get_backend()
        backend.run_evolver("opinion_reinforcement", log=logger)
        return "Opinion reinforcement completed."

    @mcp.tool()
    @graceful
    def clustering_run(distance_threshold: float = 0.1) -> str:
        """Run entity clustering/deduplication — merge duplicate entities by vector similarity."""
        backend = get_backend()
        result = backend.run_clustering()

        merged = result.get("merged_count", 0)
        clusters = result.get("clusters_found", 0)
        total = result.get("total_entities", 0)
        return (
            f"Clustering completed.\n"
            f"Total entities: {total}\n"
            f"Clusters found: {clusters}\n"
            f"Entities merged: {merged}"
        )
