"""The hosted tool allowlist (S5, design.md §4).

Hosted mode is an explicit TOOL-level allowlist, not a module list and not a
tier gate (round 2, findings 3 and 8). A new tool added to a shared module is
therefore hidden on the hosted server by default, which is the safe direction.

Every name below survived an audit of what each tool actually calls: it must
work through `RemoteBackend` alone, touch no local filesystem path, and need no
`smartmemory` core import. The exclusions and their reasons are in design.md §4.
"""

from __future__ import annotations

from typing import Any, Callable

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
CODE_TOOLS = ("code_search", "code_dead_code", "code_dependencies")
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


class _CapturingRegistrar:
    """Registers on the real server AND keeps a handle on each tool function.

    The tool modules take the server as a parameter and only ever call
    `mcp.tool(...)`, so passing this proxy registers exactly as normal through
    the public API while giving the hosted builder the original callables to
    delegate to. Nothing is patched, and nothing is registered twice.
    """

    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp
        self.captured: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        # Bare `@mcp.tool` (no parentheses).
        if args and callable(args[0]) and not kwargs:
            fn = args[0]
            self.captured[fn.__name__] = fn
            return self._mcp.tool(fn)

        def decorator(fn: Callable[..., Any]) -> Any:
            self.captured[kwargs.get("name") or fn.__name__] = fn
            return self._mcp.tool(*args, **kwargs)(fn)

        return decorator

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)


def register_module_tools(mcp: Any) -> dict[str, Callable[..., Any]]:
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
