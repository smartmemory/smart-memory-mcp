"""Adherence pattern catalog MCP tools (CORE-ADHERENCE-1).

Read and write: ``pattern_query/get/list`` and ``pattern_create/update/retire/seed``.
Compose's ``COMP-POLICY-CHECK`` and other harnesses consume ``pattern_query`` to
enforce the catalog.

**KILLED (2026-11-04):** "Read-only surface, write goes through REST" (agents
shouldn't mutate the rule catalog they are held to). OWNER D-0089
(SM-PATTERN-CRUD-1): agents write the catalog over MCP. Every write is scoped to
the backend's workspace and carries provenance (``source`` on create,
``updated_by`` on update: ``mcp:<agent id>``/``mcp:local`` locally, ``mcp:remote``
plus the server's ``@rest:<user>`` remotely). Retire is a soft retract.

The tools call backend methods only: local mode runs core over the user's own
store, remote mode calls ``/memory/patterns`` and never imports core.
``pattern_seed`` reads ``<dir>/MEMORY.md`` and only the ``.md`` files it links to
inside ``<dir>`` (no absolute link, no ``..``, no symlink out; each refusal is a
WARNING naming the link); remote mode sends the text, never a path. None of
these tools is on the hosted allowlist (``hosted/tools.py``): ``pattern_seed``
needs the client's filesystem, and the rest are not yet audited for hosted.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Union

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register the pattern catalog tools (reads and writes) with the MCP server."""

    @mcp.tool(
        title="Query adherence patterns",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_query(
        scope: Optional[str] = None,
        severity: Optional[str] = None,
        free_text: Optional[str] = None,
        limit: int = 100,
    ) -> Union[List[dict], str]:
        """Query the adherence pattern catalog.

        Filters are conjunctive. ``scope`` does case-insensitive substring
        matching against each pattern's declared scope list (so ``".py"``
        matches ``"**/*.py"`` and vice-versa). ``severity`` is one of
        ``info | warn | error``. ``free_text`` matches title, content, and
        rationale (case-insensitive substring).
        """
        return get_backend().pattern_query(
            scope=scope,
            severity=severity,
            free_text=free_text,
            limit=limit,
        )

    @mcp.tool(
        title="Get an adherence pattern",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_get(pattern_id: str) -> Union[dict, str, None]:
        """Fetch one pattern by id, or null if not found."""
        return get_backend().pattern_get(pattern_id)

    @mcp.tool(
        title="List adherence patterns",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_list(limit: int = 200) -> Union[List[dict], str]:
        """Return every active pattern (no filters)."""
        return get_backend().pattern_list(limit=limit)

    @mcp.tool(
        title="Create an adherence pattern",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_create(
        title: str,
        content: str,
        severity: str = "warn",
        scope: Optional[List[str]] = None,
        examples: Optional[List[Dict[str, Any]]] = None,
        rationale: str = "",
    ) -> Union[dict, str]:
        """Add a rule to the adherence pattern catalog of this workspace.

        ``content`` is the one-line rule; ``severity`` is ``info | warn | error``;
        ``scope`` lists glob/path fragments (empty = repo-wide); ``examples`` are
        ``{"kind": "good"|"bad", "text": ...}``. The pattern records this MCP
        process as its ``source``.
        """
        return get_backend().pattern_create(
            title=title,
            content=content,
            severity=severity,
            scope=scope,
            examples=examples,
            rationale=rationale,
        )

    @mcp.tool(
        title="Update an adherence pattern",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_update(
        pattern_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        severity: Optional[str] = None,
        scope: Optional[List[str]] = None,
        examples: Optional[List[Dict[str, Any]]] = None,
        rationale: Optional[str] = None,
    ) -> Union[dict, str]:
        """Change the given fields of an active pattern; its ``version`` goes up by one.

        Only these fields can change: the id, status, supersession and creation
        time are lifecycle-managed. ``updated_by`` records this MCP process.
        """
        fields = {
            k: v
            for k, v in (
                ("title", title),
                ("content", content),
                ("severity", severity),
                ("scope", scope),
                ("examples", examples),
                ("rationale", rationale),
            )
            if v is not None
        }
        if not fields:
            return "No fields to update: pass at least one of title, content, severity, scope, examples, rationale."
        updated = get_backend().pattern_update(pattern_id, **fields)
        if updated is None:
            return (
                f"Pattern {pattern_id!r} not found (or not active) in this workspace."
            )
        return updated

    @mcp.tool(
        title="Retire an adherence pattern",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_retire(
        pattern_id: str, reason: str = "retired via MCP"
    ) -> Union[dict, str]:
        """Retire a pattern: it is retracted (kept, with the reason, for audit) and leaves every query and list."""
        retired = get_backend().pattern_retire(pattern_id, reason=reason)
        if retired is None:
            return f"Pattern {pattern_id!r} not found (or already retired) in this workspace."
        return retired

    @mcp.tool(
        title="Seed adherence patterns from a memory directory",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def pattern_seed(dir: str) -> Union[dict, str]:
        """Create one pattern per rule linked from ``<dir>/MEMORY.md``.

        Each bullet ``- [Title](file.md) — description`` becomes a pattern; the
        linked file's text is its rationale. Only links that stay inside ``dir``
        are read: an absolute link, a ``..`` part or a symlink out of the dir is
        skipped with a WARNING naming it. Titles already in the catalog are
        skipped, so a re-run creates nothing new. Returns ``{created, skipped}``
        (each skip with its reason). A MEMORY.md that is itself a symlink out of
        ``dir`` is refused unread (``outside_dir``).
        """
        try:
            return get_backend().pattern_seed(dir)
        except FileNotFoundError:
            return f"No MEMORY.md in {dir}. Pass the directory that holds your MEMORY.md index."
        except PermissionError as e:
            # An index symlinked out of dir: refused unread, nothing sent.
            return f"Refused: {e}"

    @mcp.tool(
        title="Get violation patterns",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
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
