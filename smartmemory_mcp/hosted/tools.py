"""The hosted tool allowlist (S5, design.md §4).

Hosted mode is an explicit TOOL-level allowlist, not a module list and not a
tier gate (round 2, findings 3 and 8). A new tool added to a shared module is
therefore hidden on the hosted server by default, which is the safe direction.

Every name below survived an audit of what each tool actually calls: it must
work through `RemoteBackend` alone, touch no local filesystem path, and need no
`smartmemory` core import. The exclusions and their reasons are in design.md §4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.types import ToolAnnotations

# --- the allowlist ---------------------------------------------------------------

MEMORY_TOOLS = (
    "memory_ingest",
    "memory_search",
    "memory_recall",
    "read_around",
    "memory_get",
    "memory_explain",
    "memory_recall_pack",
    "memory_policy_bundle",
    "memory_add",
    "memory_update",
    "memory_delete",
    "memory_list",
    "memory_stats",
    "memory_distill",
    "memory_ingest_conversation",
    "memory_search_by_metadata",
    "memory_feedback",
)
CODE_TOOLS = ("code_search", "code_dead_code", "code_dependencies", "code_upload")
AGENT_TOOLS = ("agent_set_recall_profile", "agent_get_recall_profile")
REASONING_TOOLS = ("reasoning_query_traces",)
# Registered by the hosted server itself; session-scoped, no backend route of
# their own beyond the team lookup.
HOSTED_ONLY_TOOLS = ("whoami", "switch_team")

HOSTED_TOOLS: frozenset[str] = frozenset(
    MEMORY_TOOLS + CODE_TOOLS + AGENT_TOOLS + REASONING_TOOLS + HOSTED_ONLY_TOOLS
)

# Any parameter whose name reads like a filesystem location. The hosted
# container's disk is shared by every tenant, so a tool taking one of these is a
# cross-tenant read/write primitive. Asserted by a test over the LIVE schemas, so
# a newly added path-taking tool cannot reach hosted mode unnoticed.
PATH_PARAMETER_NAMES: frozenset[str] = frozenset(
    {
        "path",
        "file",
        "file_path",
        "directory",
        "memory_dir",
        "source_path",
        "archive_path",
        "files_modified",
    }
)


@dataclass(frozen=True)
class CapturedTool:
    """A registered callable and the metadata its hosted replacement must retain."""

    function: Callable[..., Any]
    title: str | None
    annotations: ToolAnnotations | None


class _CapturingRegistrar:
    """Registers on the real server AND keeps a handle on each tool function.

    The tool modules take the server as a parameter and only ever call
    `mcp.tool(...)`, so passing this proxy registers exactly as normal through
    the public API while giving the hosted builder the original callables to
    delegate to. Nothing is patched, and nothing is registered twice.
    """

    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp
        self.captured: dict[str, CapturedTool] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        # Bare `@mcp.tool` (no parentheses).
        if args and callable(args[0]) and not kwargs:
            fn = args[0]
            self.captured[fn.__name__] = CapturedTool(
                function=fn,
                title=None,
                annotations=None,
            )
            return self._mcp.tool(fn)

        def decorator(fn: Callable[..., Any]) -> Any:
            annotations = kwargs.get("annotations")
            if isinstance(annotations, dict):
                annotations = ToolAnnotations(**annotations)
            self.captured[kwargs.get("name") or fn.__name__] = CapturedTool(
                function=fn,
                title=kwargs.get("title"),
                annotations=annotations,
            )
            return self._mcp.tool(*args, **kwargs)(fn)

        return decorator

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)


def register_upload_tool(mcp: Any) -> None:
    """Register the bounded parsed-bundle tool, with no core or filesystem access."""
    from smartmemory_mcp.tools.common import get_backend, graceful

    @mcp.tool(
        title="Upload a parsed code index",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_upload(bundle: dict[str, Any]) -> dict[str, Any]:
        """Publish a client-parsed/resolved CODE-INGEST-SURFACES-1 bundle.

        Supply repo, entities, relations and optional commit_hash/parse_summary. No server
        checkout is read. Workspace is selected by the authenticated session.
        """
        if set(bundle) - {
            "repo",
            "entities",
            "relations",
            "commit_hash",
            "parse_summary",
        }:
            raise ValueError("Code upload accepts only the parsed bundle envelope")
        if (
            not isinstance(bundle.get("repo"), str)
            or not bundle["repo"].strip()
            or not bundle.get("entities")
        ):
            raise ValueError("Code upload requires repo and non-empty entities")
        if (
            len(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode())
            > 64 * 1024 * 1024
        ):
            raise ValueError("Code upload exceeds MAX_REQUEST_BODY_BYTES=67108864")
        if len({e["file_path"] for e in bundle["entities"]}) > 10000:
            raise ValueError("Code upload exceeds max_files=10000")
        backend = get_backend()
        if not backend.supports("request"):
            raise ValueError("Hosted upload requires the authenticated remote backend")
        return backend.request(
            "POST",
            "/memory/code/index",
            json=bundle,
            timeout=max(60, len(bundle["entities"]) // 50),
        )


def register_module_tools(mcp: Any) -> dict[str, CapturedTool]:
    """Register every module that CONTAINS an allowlisted tool.

    Modules bring siblings with them; the allowlist below removes those. Only
    modules with no local-only import at module scope are listed here.
    """
    from smartmemory_mcp.tools import (
        agent_tools,
        code_tools,
        memory_tools,
        reasoning_tools,
    )

    registrar = _CapturingRegistrar(mcp)
    register_upload_tool(registrar)
    memory_tools.register_free(registrar)
    memory_tools.register_pro(registrar)
    memory_tools.register_feedback(registrar)
    code_tools.register(registrar)
    agent_tools.register(registrar)
    reasoning_tools.register(registrar)
    return registrar.captured


def apply_allowlist(mcp: Any) -> None:
    """Hide everything that is not on the allowlist.

    `enable(..., only=True)` is the synchronous visibility transform
    (`providers/base.py:573-620`); the earlier `await mcp.list_tools()` diff
    could not run inside a sync builder (round 3, must-fix 4).
    """
    mcp.enable(names=set(HOSTED_TOOLS), components={"tool"}, only=True)
