"""Graph-maintenance MCP tools (CORE-GRAPH-ALIAS-RESOLVE-2 B2).

Workspace-graph consolidation operations that run over the COMPLETE graph after a
bulk ingest. Currently exposes alias resolution — merging fragmented single-token
entity aliases ("Hudson") into their multi-token canonical ("Rock Hudson"), abstaining
on collisions.

Contract: smart-memory-docs/docs/features/CORE-GRAPH-ALIAS-RESOLVE-2/resolve-aliases-contract.json
"""

import logging

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def _format_alias_report(data: dict) -> str:
    """Render an AliasResolveReport dict as an agent-readable summary string.

    Shared by the local and remote branches — both produce the same dict shape
    (`resolved`, `abstained`, `redirected_edges`, `ambiguous`, `dry_run`).

    ``dry_run`` is read from the report PAYLOAD, never from the request argument:
    the report's `dry_run` reflects what ACTUALLY executed. If the request and the
    execution diverge (e.g. the service ignores the query param and performs a real
    merge but returns `{"dry_run": false}`), the summary must follow reality and
    never claim "no changes made" when data in fact changed.
    """
    resolved = data.get("resolved", 0)
    abstained = data.get("abstained", 0)
    redirected = data.get("redirected_edges", 0)
    ambiguous = data.get("ambiguous", []) or []
    disambiguated = data.get("disambiguated", 0)
    dry_run = bool(data.get("dry_run", False))

    if dry_run:
        lead = (
            f"Dry run (preview, no changes made): would resolve {resolved} alias(es) "
            f"into canonicals, abstaining on {abstained} collision(s)"
        )
    else:
        lead = (
            f"Resolved {resolved} alias(es) into canonicals, abstained {abstained} "
            f"on collision(s), redirected {redirected} edge(s)"
        )

    if disambiguated:
        lead += f" (incl. {disambiguated} recovered by disambiguation)"
    if ambiguous:
        lead += f" [ambiguous: {', '.join(str(a) for a in ambiguous)}]"
    return lead + "."


def register(mcp):
    """Register graph-maintenance tools with the MCP server."""

    @mcp.tool()
    @graceful
    def memory_resolve_aliases(
        dry_run: bool = False, disambiguate: bool = False
    ) -> str:
        """Consolidate fragmented entity aliases over the workspace graph.

        Merges each unambiguous single-token alias node (e.g. "Hudson") into its
        multi-token canonical ("Rock Hudson") over the COMPLETE graph, redirecting
        the alias's edges onto the canonical and abstaining on collisions ("Hudson"
        with both "Rock Hudson" and "Rochelle Hudson" present). Run after a bulk
        ingest, when the full ambiguity picture is present.

        Args:
            dry_run: If True, report the resolve/abstain plan without mutating the graph.
            disambiguate: Opt-in (CORE-GRAPH-ALIAS-DISAMBIG-1, default False). Additionally recover
                colliding surfaces by structural typed-neighbor overlap — merge only on a confident,
                clear winner, else keep abstaining (never mis-merge). Extractor-dependent; helps
                LLM-extracted graphs.
        """
        backend = get_backend()

        # Try REST endpoint first (RemoteBackend) — dry_run/disambiguate are QUERY parameters.
        if backend.supports("request"):
            params: dict = {}
            if dry_run:
                params["dry_run"] = "true"
            if disambiguate:
                params["disambiguate"] = "true"
            result = backend.request(
                "POST", "/memory/graph/resolve-aliases", params=params or None
            )
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}"
            if not isinstance(result, dict):
                return "Unexpected response from API"
            return _format_alias_report(result)

        # Local backend: call resolve_aliases on the real SmartMemory (backend._mem),
        # not the MCP wrapper. workspace_id is never passed — scope derives from the
        # scope provider's read context.
        mem = getattr(backend, "_mem", None)
        if mem is None:
            return (
                "Alias resolution requires the local backend (smartmemory package) "
                "or the remote REST API."
            )
        report = mem.resolve_aliases(dry_run=dry_run, disambiguate=disambiguate)
        data = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        return _format_alias_report(data)
