"""Code indexing and search MCP tools."""

import json
import logging
import os
from typing import Any, Optional

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)

# CORE-CODE-PROVENANCE-1 Phase 2c: the hosted REST surface is parked; provenance
# blame + transcript read are local-only capabilities (the transcript JSONL and the
# captured rows live on the developer's machine).
_PARKED_MSG = (
    "{cap} is a local capability; the hosted REST surface is parked "
    "(CORE-CODE-PROVENANCE-1 Phase 2c). Run this against a local backend."
)


def _render_read_call(rh: dict) -> str:
    """Format a copyable `code_read_transcript(...)` call from a blame match's
    read_handle, so the user can chain blame -> read. Live CC (line_no None) carries
    `locate={file, norm_hash}`; Codex / CC-import carries a real `line_no`."""
    if not rh:
        return "(read handle unavailable)"
    parts = [f'source="{rh.get("source")}"', f'source_path="{rh.get("source_path")}"']
    if rh.get("line_no") is not None:
        parts.append(f"line_no={rh.get('line_no')}")
    loc = rh.get("locate") or {}
    if loc:
        parts.append(f'file="{loc.get("file")}"')
        parts.append(f'norm_hash="{loc.get("norm_hash")}"')
    return "code_read_transcript(" + ", ".join(parts) + ")"


def register(mcp):
    """Register code indexing and search tools with the MCP server."""
    from . import effects_tools

    effects_tools.register(mcp)

    @mcp.tool(
        title="Index a codebase",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_index(
        directory: str,
        repo_name: Optional[str] = None,
        exclude_dirs: Optional[str] = None,
    ) -> str:
        """Index Python and TS/JS/TSX/JSX through the shared core CodeIndexer."""
        abs_dir = os.path.abspath(directory)
        if not os.path.isdir(abs_dir):
            return f"Error: directory not found: {abs_dir}"
        repo = repo_name or os.path.basename(abs_dir)
        exclusions = (
            [d.strip() for d in exclude_dirs.split(",") if d.strip()]
            if exclude_dirs
            else None
        )
        backend = get_backend()
        if not backend.supports("ingest_code"):
            logger.warning(
                "Code indexing refused: local backend lacks core ingest_code, code edges would be lost"
            )
            return "Error: active backend does not support core code indexing"
        result = backend.ingest_code(
            directory=abs_dir,
            repo=repo,
            exclude_dirs=exclusions,
            languages=["python", "typescript"],
        )
        notice = getattr(result, "notice", "")
        summary_lines = [
            f"  Clean: {result.files_clean}, partial: {result.files_partial}, failed: {result.files_failed}",
            f"  Acceptance: {result.acceptance}, staging: {result.staging}, Publication: {result.publication}",
            "  G16 complete-generation publication is not proven by 1.x acceptance.",
        ]
        if not result.replaced:
            return "\n".join(
                summary_lines
                + [
                    getattr(result, "error_message", "")
                    or f"Error indexing: {result.errors}"
                ]
            )
        entities_stored, edges_stored = result.entities_created, result.edges_created
        files_parsed, errors = result.files_parsed, result.errors
        lines = [
            f"Indexed repo '{repo}' successfully.",
            f"  Files parsed: {files_parsed}",
            f"  Entities: {entities_stored}",
            f"  Edges: {edges_stored}",
            *summary_lines,
        ]
        if errors:
            lines.append(f"  Parse errors: {len(errors)}")
            lines.extend(f"    - {e}" for e in errors[:5])
        for diagnostic in result.diagnostics:
            if diagnostic["status"] != "clean":
                lines.append(
                    f"  {diagnostic['file_path']}: {diagnostic['status']} coverage={diagnostic['coverage']}"
                )
                lines.extend(f"    {span}" for span in diagnostic.get("spans", [])[:5])
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    @mcp.tool(
        title="Search indexed code",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_search(
        query: str,
        entity_type: Optional[str] = None,
        repo: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """Search indexed code entities by name or description."""
        backend = get_backend()

        # Try REST endpoint (RemoteBackend)
        if backend.supports("request"):
            params: dict[str, Any] = {"query": query, "limit": limit}
            if entity_type:
                params["entity_type"] = entity_type
            if repo:
                params["repo"] = repo
            result = backend.request("GET", "/memory/code/search", params=params)
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}"
            items = result if isinstance(result, list) else []
        else:
            # Local: search memory items with code type
            results = backend.search(query, top_k=limit, memory_type="code")
            items = []
            for item in results or []:
                meta = item["metadata"]
                if entity_type and meta.get("entity_type") != entity_type:
                    continue
                if repo and meta.get("repo") != repo:
                    continue
                items.append(
                    {
                        **{
                            key: meta.get(key)
                            for key in (
                                "item_id",
                                "qualified_name",
                                "end_line_number",
                                "byte_start",
                                "byte_end",
                                "content_hash",
                                "source_snapshot",
                                "call_evidence",
                            )
                            if key in meta
                        },
                        "item_id": item.get("item_id", meta.get("item_id", "")),
                        "name": meta.get("name", "?"),
                        "entity_type": meta.get("entity_type", "?"),
                        "file_path": meta.get("file_path", "?"),
                        "line_number": meta.get("line_number", "?"),
                        "repo": meta.get("repo", ""),
                        "docstring": item["content"][:200],
                        "http_method": meta.get("http_method", ""),
                        "http_path": meta.get("http_path", ""),
                    }
                )

        if not items:
            return f"No code entities found for: {query}"

        lines = [f"Found {len(items)} code entities for '{query}':\n"]
        for i, item in enumerate(items, 1):
            name = item.get("name", "?")
            etype = item.get("entity_type", "?")
            fpath = item.get("file_path", "?")
            lineno = item.get("line_number", "?")
            repo_name = item.get("repo", "")
            prefix = f"[{repo_name}] " if repo_name else ""
            line = f"{i}. {prefix}{etype}: {name}  ({fpath}:{lineno})"
            if etype == "route":
                method = item.get("http_method", "")
                path = item.get("http_path", "")
                if method and path:
                    line += f"  {method} {path}"
            docstring = item.get("docstring", "")
            if docstring:
                line += f"\n   {docstring}"
            if not item.get("content_hash") or "call_evidence" not in item:
                logger.warning(
                    "Code entity %s lacks source spans and call confidence; re-index to restore source grounding",
                    item.get("item_id", name),
                )
            evidence = {
                key: item[key]
                for key in (
                    "item_id",
                    "qualified_name",
                    "end_line_number",
                    "byte_start",
                    "byte_end",
                    "content_hash",
                    "source_snapshot",
                    "call_evidence",
                )
                if key in item
            }
            if evidence:
                line += "\n   Evidence: " + json.dumps(evidence, ensure_ascii=False)
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool(
        title="Find code authorship",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_blame(
        commit: str = "",
        file: str = "",
        line: int = 0,
        repo: str = "",
        ref: str = "HEAD",
        top_k: int = 5,
    ) -> str:
        """Which captured session authored this code? Given a git commit (or a
        file + line) in a local repo, find the Claude Code / Codex session whose
        edits structurally produced that code (CORE-CODE-PROVENANCE-1 Phase 2b).

        Local-backend only: provenance is captured into the local store and git
        runs against the local working tree. The hosted REST surface is Phase 2c.
        """
        if not repo:
            return "Error: `repo` is required (path to the local git repository)."
        if not commit and not (file and line):
            return "Error: provide either `commit`, or both `file` and `line`."

        backend = get_backend()
        if not backend.supports("blame_code"):
            return _PARKED_MSG.format(cap="Provenance blame")

        try:
            res = backend.blame_code(
                commit=commit or None,
                file=file or None,
                line=line or None,
                repo=repo,
                ref=ref,
                top_k=top_k,
            )
        except ValueError as e:  # git error (unknown commit / not a repo / bad target)
            return f"Error (git): {e}"

        status = res.get("status")
        matches = res.get("matches") or []
        if status in {"no_indexable_content", "merge_no_direct_changes"}:
            return f"No attributable code in target ({status})."
        if not matches:
            return "No captured session authored this code (no provenance match)."

        target = res.get("query", {})
        head = "Authoring session(s) for " + (
            f"commit {target.get('commit', '')[:10]}"
            if target.get("commit")
            else f"{target.get('file', '?')}:{target.get('line', '?')}"
        )
        if status == "no_clear_author":
            head += "  [no clear author — candidates below]"
        lines = [head + ":\n"]
        for i, m in enumerate(matches, 1):
            unconf = "  (repo-unconfirmed)" if m.get("repo_unconfirmed") else ""
            cov = m.get("target_coverage")
            cov_s = f"{cov:.0%}" if isinstance(cov, (int, float)) else "?"
            surv = (m.get("survival") or {}).get("overall")
            surv_s = f", survival {surv:.0%}" if isinstance(surv, (int, float)) else ""
            lines.append(
                f"{i}. [{m.get('source')}] session {m.get('session_id')}{unconf}\n"
                f"   method={m.get('method')}  coverage={cov_s}{surv_s}\n"
                f"   transcript: {m.get('source_path')}\n"
                f"   read: {_render_read_call(m.get('read_handle') or {})}"
            )
        amb = res.get("ambiguous_spans") or []
        if amb:
            lines.append(f"\n{len(amb)} ambiguous span(s) (tied candidates).")
        return "\n".join(lines)

    @mcp.tool(
        title="Read an authoring transcript",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_read_transcript(
        source_path: str = "",
        line_no: int = 0,
        source: str = "cc",
        file: str = "",
        norm_hash: str = "",
        char_budget: int = 20000,
        next_line: int = 0,
        prev_line: int = 0,
    ) -> str:
        """Read the conversation that authored a span of code (CORE-CODE-PROVENANCE-1
        Phase 2c) — the *read* half of the blame->read chain. Copy the
        `code_read_transcript(...)` call that `code_blame` prints for a match: it
        renders a centered window of the authoring Claude Code / Codex transcript.

        Local-backend only: the transcript JSONL lives on the developer's machine.
        The hosted REST surface is parked. For live CC (no `line_no`), pass the
        match's `file` + `norm_hash` so the authoring edit can be located. To page,
        re-call with `next_line` (or `prev_line`) from the previous continue-cursor —
        the window then extends directionally instead of recentering.
        """
        if not source_path:
            return "Error: `source_path` is required (copy it from a code_blame read handle)."
        if source not in ("cc", "codex"):
            return f"Error: unknown source {source!r} (expected 'cc' or 'codex')."

        backend = get_backend()
        if not backend.supports("read_transcript_centered"):
            return _PARKED_MSG.format(cap="Transcript reading")

        # Directional paging: a cursor makes center_over_rendered extend forward/back
        # from the requested edge instead of recentering (the footer advertises this).
        cursor = None
        effective_line = line_no or None
        if next_line:
            cursor, effective_line = {"next_line": next_line}, next_line
        elif prev_line:
            cursor, effective_line = {"prev_line": prev_line}, prev_line

        locate = (
            {"file": file, "norm_hash": norm_hash} if (file and norm_hash) else None
        )
        try:
            res = backend.read_transcript_centered(
                source_path=source_path,
                line_no=effective_line,
                source=source,
                locate=locate,
                cursor=cursor,
                char_budget=char_budget,
            )
        except (FileNotFoundError, OSError, ValueError) as e:
            return f"Error: {e}"

        handle = res.get("handle") or {}
        cur = res.get("continue_cursor") or {}
        head = (
            f"Transcript [{handle.get('source')}] session {handle.get('session_id')} "
            f"@ line {handle.get('line_no')}:"
        )
        footer = (
            f"(continue: prev_line={cur.get('prev_line')}, next_line={cur.get('next_line')}; "
            f"{res.get('chars_used')}/{res.get('char_budget')} chars)"
        )
        return f"{head}\n\n{res.get('window', '')}\n\n{footer}"

    @mcp.tool(
        title="Find dead code",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_dead_code(
        repo: str,
        exclude_decorators: Optional[str] = None,
        limit: int = 50,
    ) -> str:
        """Find potentially dead callable entities, components and hooks in an indexed codebase."""
        backend = get_backend()

        if backend.supports("request"):
            params: dict[str, Any] = {"repo": repo, "limit": limit}
            if exclude_decorators:
                params["exclude_decorators"] = exclude_decorators
            result = backend.request("GET", "/memory/code/dead-code", params=params)
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}"
            if not isinstance(result, dict):
                return "Unexpected response from API"
            dead = result.get("dead_functions", [])
            count = result.get("count", len(dead))
        else:
            result = backend.code_dead_code(
                repo=repo, exclude_decorators=exclude_decorators, limit=limit
            )
            dead = result.get("dead_functions", [])
            count = result.get("count", len(dead))

        if not dead:
            return f"No dead code found in repo '{repo}'."

        lines = [f"Found {count} potentially unused callable entities in '{repo}':\n"]
        for i, item in enumerate(dead, 1):
            name = item.get("name", "?")
            fpath = item.get("file_path", "?")
            lineno = item.get("line_number", "?")
            dec = item.get("decorators", "")
            kind = item.get("entity_type", "function")
            line = f"{i}. {name} ({kind})  ({fpath}:{lineno})"
            if dec:
                line += f"  [{dec}]"
            lines.append(line)
        lines.append(f"\nTotal: {count} potentially dead callable entities")
        return "\n".join(lines)

    @mcp.tool(
        title="Trace code dependencies",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_dependencies(
        entity_name: str,
        direction: str = "both",
        repo: Optional[str] = None,
    ) -> str:
        """Trace code dependencies -- what calls/imports/inherits what."""
        backend = get_backend()

        if backend.supports("request"):
            params: dict[str, Any] = {
                "entity_name": entity_name,
                "direction": direction,
            }
            if repo:
                params["repo"] = repo
            result = backend.request("GET", "/memory/code/dependencies", params=params)
            if isinstance(result, dict) and "error" in result:
                return f"Error: {result['error']}"
            if not isinstance(result, dict):
                return "Unexpected response from API"
        else:
            return "Dependency analysis requires the remote backend (REST API). Use code_search to find entities locally."

        root = result.get("root", {})
        dependents = result.get("dependents", [])
        dependencies = result.get("dependencies", [])

        lines = []
        if root:
            etype = root.get("entity_type", "?")
            fpath = root.get("file_path", "?")
            lineno = root.get("line_number", "?")
            lines.append(f"Entity: {entity_name} ({etype}) at {fpath}:{lineno}")
        else:
            lines.append(f"Entity: {entity_name}")

        if direction in ("dependencies", "both") and dependencies:
            lines.append(f"\nDependencies ({len(dependencies)} -- what this uses):")
            for dep in dependencies:
                rel = dep.get("edge_type", "?")
                target = dep.get("name", dep.get("item_id", "?"))
                target_type = dep.get("entity_type", "")
                suffix = f" ({target_type})" if target_type else ""
                lines.append(f"  {rel} -> {target}{suffix}")
        elif direction in ("dependencies", "both"):
            lines.append("\nDependencies: none")

        if direction in ("dependents", "both") and dependents:
            lines.append(f"\nDependents ({len(dependents)} -- what uses this):")
            for dep in dependents:
                rel = dep.get("edge_type", "?")
                source = dep.get("name", dep.get("item_id", "?"))
                source_type = dep.get("entity_type", "")
                suffix = f" ({source_type})" if source_type else ""
                lines.append(f"  {source}{suffix} {rel} -> this")
        elif direction in ("dependents", "both"):
            lines.append("\nDependents: none")

        return "\n".join(lines)
