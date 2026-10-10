"""Advertise optional tools only when the active backend implements them."""

import logging

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from smartmemory_mcp.backends.interface import LOCAL_BROKEN_TOOLS
from smartmemory_mcp.tools.common import get_backend

logger = logging.getLogger(__name__)

# Keep the mapping explicit: REST's `request` is a branch with local fallbacks,
# whereas these tools depend on a specific backend operation in every mode.
TOOL_CAPABILITIES = {
    "memory_ingest_document": "ingest_document",
    "code_blame": "blame_code",
    "code_read_transcript": "read_transcript_centered",
    "peer_chat": "peer_chat",
    # MCP-LOCAL-BROKEN-TOOLS-1: one capability per tool, named after the tool. Local
    # declares them unsupported; the bug behind each is in LOCAL_BROKEN_TOOLS.
    **{tool: tool for tool in LOCAL_BROKEN_TOOLS},
}


class BackendCapabilityMiddleware(Middleware):
    """Resolve at listing/call time, preserving lazy backend initialization."""

    def __init__(self):
        self._warned_hidden: frozenset[str] = frozenset()

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        if not any(tool.name in TOOL_CAPABILITIES for tool in tools):
            return tools
        backend = get_backend()
        visible = [
            tool
            for tool in tools
            if tool.name not in TOOL_CAPABILITIES
            or backend.supports(TOOL_CAPABILITIES[tool.name])
        ]
        self._warn_hidden_broken(tools, visible)
        return visible

    def _warn_hidden_broken(self, tools, visible):
        """Say once which known-broken tools this backend hides, and why."""
        kept = {tool.name for tool in visible}
        hidden = frozenset(
            tool.name
            for tool in tools
            if tool.name in LOCAL_BROKEN_TOOLS and tool.name not in kept
        )
        if not hidden or hidden == self._warned_hidden:
            return
        self._warned_hidden = hidden
        logger.warning(
            "Hiding %d tools that fail on this backend (MCP-LOCAL-BROKEN-TOOLS-1): %s",
            len(hidden),
            "; ".join(
                f"{name} ({LOCAL_BROKEN_TOOLS[name]})" for name in sorted(hidden)
            ),
        )

    async def on_call_tool(self, context, call_next):
        capability = TOOL_CAPABILITIES.get(context.message.name)
        if capability and not get_backend().supports(capability):
            raise ToolError(
                f"{context.message.name} is not supported by the active backend."
            )
        return await call_next(context)
