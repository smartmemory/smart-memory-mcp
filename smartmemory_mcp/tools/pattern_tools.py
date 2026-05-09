"""Adherence pattern catalog MCP tools (CORE-ADHERENCE-1).

Read-only surface — write goes through REST. Compose's ``COMP-POLICY-CHECK``
and other harnesses consume ``pattern_query`` to enforce the catalog.
"""

import logging
from typing import Any, List, Optional

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def _pattern_to_dict(p: Any) -> dict:
    """Serialize a Pattern dataclass for JSON-friendly MCP output."""
    if hasattr(p, "to_dict"):
        return p.to_dict()
    return dict(p)


def register(mcp):
    """Register read-only pattern tools with the MCP server."""

    @mcp.tool()
    @graceful
    def pattern_query(
        scope: Optional[str] = None,
        severity: Optional[str] = None,
        free_text: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """Query the adherence pattern catalog.

        Filters are conjunctive. ``scope`` does case-insensitive substring
        matching against each pattern's declared scope list (so ``".py"``
        matches ``"**/*.py"`` and vice-versa). ``severity`` is one of
        ``info | warn | error``. ``free_text`` matches title, content, and
        rationale (case-insensitive substring).
        """
        from smartmemory.adherence import PatternManager

        manager = PatternManager(get_backend())
        results = manager.query(
            scope=scope,
            severity=severity,
            free_text=free_text,
            limit=limit,
        )
        return [_pattern_to_dict(p) for p in results]

    @mcp.tool()
    @graceful
    def pattern_get(pattern_id: str) -> Optional[dict]:
        """Fetch one pattern by id, or null if not found."""
        from smartmemory.adherence import PatternManager

        manager = PatternManager(get_backend())
        inst = manager.get(pattern_id)
        if inst is None:
            return None
        return _pattern_to_dict(inst)

    @mcp.tool()
    @graceful
    def pattern_list(limit: int = 200) -> List[dict]:
        """Return every active pattern (no filters)."""
        from smartmemory.adherence import PatternManager

        manager = PatternManager(get_backend())
        results = manager.queries.list_all(limit=limit)
        return [_pattern_to_dict(p) for p in results]
