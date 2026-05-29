"""Adherence pattern catalog MCP tools (CORE-ADHERENCE-1).

Read-only surface — write goes through REST. Compose's ``COMP-POLICY-CHECK``
and other harnesses consume ``pattern_query`` to enforce the catalog.
"""

import logging
import os
from typing import Any, List, Optional, Union

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

    @mcp.tool()
    @graceful
    def memory_get_violation_patterns(
        rule_id: Optional[str] = None,
        rule_type: str = "feedback",
        memory_dir: Optional[str] = None,
    ) -> Union[List[dict], str]:
        """Return the ``## Detection patterns`` catalog from local rule files.

        Distinct from ``pattern_query``/``pattern_get``/``pattern_list`` (which
        read the graph-backed Pattern *catalog*): this reads the per-rule
        ``## Detection patterns`` markdown blocks from ``{rule_type}_*.md`` files
        on the local filesystem. Harnesses (Compose's ``COMP-POLICY-CHECK``, raw
        Claude Code, Cursor) fetch this once per session and run their own
        pre-response check — scan a candidate response against ``patterns``,
        suppress when a recent user turn matches a ``suppression_signal``.

        Args:
            rule_id: if given, return only the rule whose ``name`` matches.
            rule_type: rule-file prefix to scan (default ``feedback``).
            memory_dir: directory of rule files. Falls back to the
                ``SMARTMEMORY_RULES_DIR`` env var when omitted.

        Returns:
            A list of ``RulePatterns`` dicts, or an explanatory string when no
            ``memory_dir`` can be resolved (never a silently-empty list).
        """
        from smartmemory.adherence import load_rule_patterns

        resolved = memory_dir or os.environ.get("SMARTMEMORY_RULES_DIR")
        if not resolved:
            return (
                "No memory_dir resolved. Pass memory_dir=<path to your rule "
                "files> or set the SMARTMEMORY_RULES_DIR environment variable."
            )

        # Filesystem-backed: a bad path is a path error, not a backend outage.
        # Catch it here so @graceful doesn't mislabel it "backend not reachable".
        try:
            results = load_rule_patterns(resolved, rule_type=rule_type)
        except FileNotFoundError:
            return (
                f"Rule directory not found: {resolved}. Pass a valid memory_dir "
                "or set SMARTMEMORY_RULES_DIR to your rule-files directory."
            )

        if rule_id is not None:
            results = [rp for rp in results if rp.name == rule_id]
        return [rp.to_dict() for rp in results]
