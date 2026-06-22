"""Code indexing and search MCP tools."""

import json
import logging
import os
from typing import Any, Optional

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register code indexing and search tools with the MCP server."""

    @mcp.tool()
    @graceful
    def code_index(
        directory: str,
        repo_name: Optional[str] = None,
        exclude_dirs: Optional[str] = None,
    ) -> str:
        """Index a Python codebase into SmartMemory's knowledge graph."""
        from smartmemory_mcp.code_parser import CodeParser, collect_python_files, DEFAULT_EXCLUDE_DIRS

        abs_dir = os.path.abspath(directory)
        if not os.path.isdir(abs_dir):
            return f"Error: directory not found: {abs_dir}"

        repo = repo_name or os.path.basename(abs_dir)

        if exclude_dirs:
            excl = set(d.strip() for d in exclude_dirs.split(",") if d.strip())
        else:
            excl = DEFAULT_EXCLUDE_DIRS

        py_files = collect_python_files(abs_dir, exclude_dirs=excl)
        if not py_files:
            return f"No Python files found in {abs_dir}"

        parser = CodeParser(repo=repo, repo_root=abs_dir)
        all_entities = []
        all_relations = []
        all_errors = []

        for fpath in py_files:
            pr = parser.parse_file(fpath)
            all_entities.extend(pr.entities)
            all_relations.extend(pr.relations)
            all_errors.extend(pr.errors)

        backend = get_backend()

        # Try REST endpoint first (RemoteBackend)
        if hasattr(backend, "request"):
            payload = {
                "repo": repo,
                "entities": [e.to_dict() for e in all_entities],
                "relations": [r.to_dict() for r in all_relations],
            }
            timeout = max(60, len(all_entities) // 50)
            result = backend.request("POST", "/memory/code/index", timeout=timeout, json=payload)
            if isinstance(result, dict) and "error" not in result:
                entities_stored = result.get("entities_created", len(all_entities))
                edges_stored = result.get("edges_created", len(all_relations))
            else:
                return f"Error indexing via API: {result}"
        else:
            # Local backend: store each entity as a memory item
            entities_stored = 0
            edges_stored = 0
            for entity in all_entities:
                try:
                    content = f"Code entity: {entity.name} ({entity.entity_type}) in {entity.file_path}:{entity.line_number}"
                    if entity.docstring:
                        content += f"\n{entity.docstring}"
                    backend.add(
                        content=content,
                        memory_type="code",
                        metadata={
                            "entity_type": entity.entity_type,
                            "name": entity.name,
                            "file_path": entity.file_path,
                            "line_number": entity.line_number,
                            "repo": repo,
                            "item_id": entity.item_id,
                            "decorators": entity.decorators,
                            "http_method": entity.http_method,
                            "http_path": entity.http_path,
                        },
                    )
                    entities_stored += 1
                except Exception as e:
                    logger.warning(f"Failed to store entity {entity.name}: {e}")

        lines = [
            f"Indexed repo '{repo}' successfully.",
            f"  Files parsed: {len(py_files)}",
            f"  Entities: {entities_stored}",
            f"  Edges: {edges_stored}",
        ]
        if all_errors:
            lines.append(f"  Parse errors: {len(all_errors)}")
            for e in all_errors[:5]:
                lines.append(f"    - {e}")
            if len(all_errors) > 5:
                lines.append(f"    ... and {len(all_errors) - 5} more")
        return "\n".join(lines)

    @mcp.tool()
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
        if hasattr(backend, "request"):
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
            for item in (results or []):
                meta = item["metadata"]
                if entity_type and meta.get("entity_type") != entity_type:
                    continue
                if repo and meta.get("repo") != repo:
                    continue
                items.append({
                    "name": meta.get("name", "?"),
                    "entity_type": meta.get("entity_type", "?"),
                    "file_path": meta.get("file_path", "?"),
                    "line_number": meta.get("line_number", "?"),
                    "repo": meta.get("repo", ""),
                    "docstring": item["content"][:200],
                    "http_method": meta.get("http_method", ""),
                    "http_path": meta.get("http_path", ""),
                })

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
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool()
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
        if hasattr(backend, "request"):
            return "Provenance blame requires a local backend (the REST surface is Phase 2c)."

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
            f"commit {target.get('commit', '')[:10]}" if target.get("commit")
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
                f"   transcript: {m.get('source_path')}"
            )
        amb = res.get("ambiguous_spans") or []
        if amb:
            lines.append(f"\n{len(amb)} ambiguous span(s) (tied candidates).")
        return "\n".join(lines)

    @mcp.tool()
    @graceful
    def code_dead_code(
        repo: str,
        exclude_decorators: Optional[str] = None,
        limit: int = 50,
    ) -> str:
        """Find potentially dead (unreferenced) functions in an indexed codebase."""
        backend = get_backend()

        if hasattr(backend, "request"):
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
            return "Dead code analysis requires the remote backend (REST API). Use code_search to find entities locally."

        if not dead:
            return f"No dead code found in repo '{repo}'."

        lines = [f"Found {count} potentially unused functions in '{repo}':\n"]
        for i, item in enumerate(dead, 1):
            name = item.get("name", "?")
            fpath = item.get("file_path", "?")
            lineno = item.get("line_number", "?")
            dec = item.get("decorators", "")
            line = f"{i}. {name}  ({fpath}:{lineno})"
            if dec:
                line += f"  [{dec}]"
            lines.append(line)
        lines.append(f"\nTotal: {count} potentially dead functions")
        return "\n".join(lines)

    @mcp.tool()
    @graceful
    def code_dependencies(
        entity_name: str,
        direction: str = "both",
        repo: Optional[str] = None,
    ) -> str:
        """Trace code dependencies -- what calls/imports/inherits what."""
        backend = get_backend()

        if hasattr(backend, "request"):
            params: dict[str, Any] = {"entity_name": entity_name, "direction": direction}
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
