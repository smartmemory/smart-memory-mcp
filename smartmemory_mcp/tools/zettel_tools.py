"""Zettelkasten MCP tools (extracted from service graph_tools.py)."""

import logging

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def _get_zettel_memory():
    """Get ZettelMemory over the local backend's own store (its SmartMemory's graph and scope).

    Returns (ZettelMemory, None) on success, (None, error_message) on failure. SM-LOCAL-TOOLS-1:
    ``ZettelMemory(memory=...)`` was never a core signature, and the plain constructor builds
    its own graph on the configured default backend, not the Lite store.
    """
    from smartmemory.memory.types.zettel_memory import ZettelMemory

    backend = get_backend()

    # LocalBackend exposes _mem; RemoteBackend does not
    mem = getattr(backend, "_mem", None)
    if mem is None:
        return (
            None,
            "Zettel tools require local backend. Remote mode does not support graph-based zettel operations.",
        )

    return ZettelMemory.for_memory(mem), None


def register(mcp):
    """Register Zettelkasten tools with the MCP server."""

    @mcp.tool(
        title="Find memory backlinks",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def zettel_backlinks(
        note_id: str,
    ) -> str:
        """Get notes that link TO this note (backlinks)."""
        zettel, err = _get_zettel_memory()
        if err:
            return err

        backlinks = zettel.backlinks.get_backlinks(note_id)

        if not backlinks:
            return f"No backlinks found for {note_id}"

        output = [f"Backlinks for {note_id} ({len(backlinks)}):"]
        for bl in backlinks:
            output.append(f"  - {bl}")
        return "\n".join(output)

    @mcp.tool(
        title="Find memory connections",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def zettel_connections(
        note_id: str,
    ) -> str:
        """Get all connections (backlinks + forward links) for a note."""
        zettel, err = _get_zettel_memory()
        if err:
            return err

        connections = zettel.backlinks.get_all_connections(note_id)

        if not connections:
            return f"No connections found for {note_id}"

        output = [f"Connections for {note_id} ({len(connections)}):"]
        for conn in connections:
            output.append(f"  - {conn}")
        return "\n".join(output)

    @mcp.tool(
        title="Find memory clusters",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def zettel_clusters(
        min_size: int = 3,
    ) -> str:
        """Detect knowledge clusters in your Zettelkasten."""
        zettel, err = _get_zettel_memory()
        if err:
            return err

        clusters = zettel.structure.detect_knowledge_clusters(min_cluster_size=min_size)

        if not clusters:
            return "No knowledge clusters detected."

        output = [f"Found {len(clusters)} knowledge clusters:\n"]
        for c in clusters:
            output.append(
                f"  - Cluster {c.cluster_id}: {len(c.note_ids)} notes, "
                f"density={c.connection_density:.2f}, "
                f"concepts={', '.join(c.central_concepts[:3])}"
            )
        return "\n".join(output)

    @mcp.tool(
        title="Discover related memories",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def zettel_discover(
        note_id: str,
        min_surprise: float = 0.5,
    ) -> str:
        """Find unexpected connections (serendipitous discovery) for a note."""
        zettel, err = _get_zettel_memory()
        if err:
            return err

        discoveries = zettel.discovery.discover_missing_connections(note_id)

        filtered = [(d[0], d[1]) for d in discoveries if d[1] >= min_surprise]

        if not filtered:
            return f"No surprising connections found for {note_id}"

        output = [f"Discoveries for {note_id} ({len(filtered)}):\n"]
        for target_id, score in filtered:
            try:
                note = zettel.get(target_id)
            except Exception:
                logger.warning(
                    "zettel_discover could not load note %s; showing its id",
                    target_id,
                    exc_info=True,
                )
                note = None
            title = target_id
            if note is not None:
                note_meta = getattr(note, "metadata", None)
                if isinstance(note_meta, dict):
                    title = note_meta.get("title", target_id)
                elif note_meta:
                    logger.warning(
                        "zettel_discover note %s has %s metadata, not a dict; its "
                        "title was not read",
                        target_id,
                        type(note_meta).__name__,
                    )
            output.append(f"  - {title} (surprise: {score:.2f})")
        return "\n".join(output)
