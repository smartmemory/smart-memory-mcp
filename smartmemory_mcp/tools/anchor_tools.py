"""Anchor management MCP tools."""

import logging
from typing import List, Optional

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register anchor lifecycle tools with the MCP server."""

    @mcp.tool(
        title="Set a session anchor",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_anchor_set(
        content: str,
        anchor_type: str = "spec",
        session_id: str = "",
    ) -> str:
        """Set a spec anchor to pin a requirement or constraint for the session."""
        from smartmemory.anchors.manager import AnchorManager

        backend = get_backend()
        manager = AnchorManager(backend)
        item_id = manager.set(content, anchor_type, session_id)
        return f"Anchor set: {item_id} (type={anchor_type}, session={session_id})"

    @mcp.tool(
        title="List session anchors",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_anchor_list(
        session_id: str = "",
        anchor_type: Optional[str] = None,
    ) -> str:
        """List active anchors for a session."""
        from smartmemory.anchors.manager import AnchorManager

        backend = get_backend()
        manager = AnchorManager(backend)
        anchors = manager.list(session_id, anchor_type)
        if not anchors:
            return "No active anchors."
        lines = [f"Active anchors ({len(anchors)}):"]
        for a in anchors:
            lines.append(
                f"  [{a.get('anchor_type', '?')}] {a.get('anchor_id', '?')}: "
                f"{a.get('content', '')[:100]}"
            )
        return "\n".join(lines)

    @mcp.tool(
        title="Clear session anchors",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_anchor_clear(
        session_id: str = "",
        anchor_type: Optional[str] = None,
    ) -> str:
        """Clear (deactivate) anchors for a session."""
        from smartmemory.anchors.manager import AnchorManager

        backend = get_backend()
        manager = AnchorManager(backend)
        count = manager.clear(session_id, anchor_type)
        type_label = f" (type={anchor_type})" if anchor_type else ""
        return f"Cleared {count} anchor(s){type_label} for session={session_id}."

    @mcp.tool(
        title="Graduate session anchors",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_anchor_graduate(
        session_id: str = "",
    ) -> str:
        """Graduate active anchors to persistent decisions."""
        from smartmemory.anchors.manager import AnchorManager

        backend = get_backend()
        manager = AnchorManager(backend)
        decision_ids = manager.graduate(session_id)
        if not decision_ids:
            return "No active anchors to graduate."
        lines = [f"Graduated {len(decision_ids)} anchor(s) to decisions:"]
        for did in decision_ids:
            lines.append(f"  {did}")
        return "\n".join(lines)

    @mcp.tool(
        title="Check anchor drift",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_anchor_check_drift(
        session_id: str = "",
        recent_outputs: Optional[List[str]] = None,
    ) -> str:
        """Check if recent outputs have drifted from anchored requirements."""
        from smartmemory.anchors.manager import AnchorManager

        backend = get_backend()
        manager = AnchorManager(backend)
        reports = manager.check_drift(session_id, recent_outputs or [])
        if not reports:
            return "No drift detected (no active anchors or no outputs to check)."
        lines = [f"Drift report ({len(reports)} anchor(s)):"]
        for r in reports:
            lines.append(
                f"  {r.anchor_id}: severity={r.severity}, "
                f"drift_score={r.drift_score:.2f}"
            )
            if r.missing_keywords:
                lines.append(f"    missing: {', '.join(r.missing_keywords)}")
            lines.append(f"    anchor: {r.anchor_content[:80]}")
        return "\n".join(lines)
