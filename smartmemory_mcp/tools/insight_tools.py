"""Insight and maintenance MCP tools — health, reflection, plugins, personalization."""

import logging
from typing import Optional

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register insight tools with the MCP server."""

    @mcp.tool(
        title="Get memory health",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_health() -> str:
        """Get memory health summary — item counts, health score, and detected issues."""
        backend = get_backend()

        summary = backend.summary()

        items_by_type = {}
        total_items = 0
        for memory_type, data in summary.items():
            if isinstance(data, dict) and "count" in data:
                count = data["count"]
                items_by_type[memory_type] = count
                total_items += count

        orphan_count = len(backend.orphaned_notes())
        stale_count = len(backend.find_old_notes(90))

        health_score = 1.0
        if total_items > 0:
            orphan_ratio = orphan_count / total_items
            stale_ratio = stale_count / total_items
            health_score = max(0.0, 1.0 - (orphan_ratio * 0.3) - (stale_ratio * 0.2))
        health_score = round(health_score, 2)

        parts = [
            f"Memory Health Score: {health_score}",
            f"Total items: {total_items}",
            "\nBy type:",
        ]
        for mtype, count in sorted(items_by_type.items()):
            parts.append(f"  - {mtype}: {count}")

        issues = []
        if orphan_count > 0:
            issues.append(f"  - {orphan_count} orphaned memories")
        if stale_count > 0:
            issues.append(f"  - {stale_count} stale memories (>90 days)")

        if issues:
            parts.append("\nIssues:")
            parts.extend(issues)
        else:
            parts.append("\nNo issues detected.")

        return "\n".join(parts)

    @mcp.tool(
        title="Reflect on memory patterns",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_reflect(top_k: int = 10) -> str:
        """Reflect on memory patterns — identify key concepts, top entities, and dominant topics."""
        backend = get_backend()
        raw_reflection = backend.reflect(top_k=top_k)

        key_concepts = []
        dominant_topics = []

        for memory_type, data in raw_reflection.items():
            if isinstance(data, dict):
                top_keywords = data.get("top_keywords", [])
                for kw in top_keywords:
                    if isinstance(kw, (list, tuple)) and len(kw) >= 2:
                        key_concepts.append((kw[0], kw[1]))
                    elif isinstance(kw, str):
                        key_concepts.append((kw, 1))

                if data.get("total_items", 0) > 0:
                    dominant_topics.append(memory_type)

        key_concepts.sort(key=lambda x: x[1], reverse=True)
        key_concepts = key_concepts[:top_k]

        parts = ["Memory Reflection:\n"]

        if dominant_topics:
            parts.append(f"Dominant topics: {', '.join(dominant_topics)}")

        if key_concepts:
            parts.append("\nKey concepts:")
            for term, freq in key_concepts:
                parts.append(f"  - {term} ({freq})")
        else:
            parts.append("No key concepts detected yet.")

        return "\n".join(parts)

    @mcp.tool(
        title="Get maintenance status",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_maintenance_status(
        stale_threshold_days: int = 90,
        low_confidence_threshold: float = 0.5,
    ) -> str:
        """Get maintenance status — orphaned, stale, and low-confidence memory counts."""
        backend = get_backend()

        orphaned = backend.orphaned_notes()
        stale = backend.find_old_notes(stale_threshold_days)

        parts = [
            "Maintenance Status:\n",
            f"Orphaned memories: {len(orphaned)}",
            f"Stale memories (>{stale_threshold_days} days): {len(stale)}",
        ]

        if orphaned:
            preview_ids = [i["item_id"] for i in orphaned[:5]]
            parts.append(f"  Preview: {', '.join(preview_ids)}")

        if stale:
            preview_ids = [i["item_id"] for i in stale[:5]]
            parts.append(f"  Preview: {', '.join(preview_ids)}")

        return "\n".join(parts)

    @mcp.tool(
        title="List available plugins",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_plugins() -> str:
        """List available extractors and enrichers."""
        try:
            from smartmemory.plugins.manager import get_plugin_manager

            manager = get_plugin_manager()
            registry = manager.registry
        except ImportError:
            return "Plugin system not initialized."

        parts = ["Available Plugins:\n"]

        try:
            extractor_names = registry.list_plugins("extractor")
            parts.append(f"Extractors ({len(extractor_names)}):")
            for name in extractor_names:
                parts.append(f"  - {name}")
        except Exception as e:
            logger.debug(f"Could not list extractors: {e}")
            parts.append("Extractors: unavailable")

        try:
            enricher_names = registry.list_plugins("enricher")
            parts.append(f"\nEnrichers ({len(enricher_names)}):")
            for name in enricher_names:
                parts.append(f"  - {name}")
        except Exception as e:
            logger.debug(f"Could not list enrichers: {e}")
            parts.append("\nEnrichers: unavailable")

        return "\n".join(parts)

    @mcp.tool(
        title="Set personalization settings",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_personalize(
        traits: Optional[dict] = None,
        preferences: Optional[dict] = None,
    ) -> str:
        """Apply personalization settings — traits and preferences for memory retrieval."""
        backend = get_backend()
        backend.personalize(
            user_id="mcp-user",
            traits=traits or {},
            preferences=preferences or {},
        )
        return "Personalization applied successfully."

    @mcp.tool(
        title="Process memory feedback",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_feedback(
        feedback: Optional[dict] = None,
        memory_type: str = "semantic",
    ) -> str:
        """Update memory system based on user feedback."""
        backend = get_backend()
        backend.update_from_feedback(feedback=feedback or {}, memory_type=memory_type)
        return "Feedback processed successfully."

    @mcp.tool(
        title="Ground a memory item",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def insight_ground(item_id: str, source_url: str) -> str:
        """Ground a memory item to an external source URL for provenance."""
        backend = get_backend()

        item = backend.get(item_id)
        if not item:
            return f"Memory item not found: {item_id}"

        backend.ground(item_id=item_id, source_url=source_url)
        return f"Memory item {item_id} grounded to {source_url}"
