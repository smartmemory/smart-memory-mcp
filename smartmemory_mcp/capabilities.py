"""Advertise optional tools only when the active backend implements them."""

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from smartmemory_mcp.tools.common import get_backend

# Keep the mapping explicit: REST's `request` is a branch with local fallbacks,
# whereas these tools depend on a specific backend operation in every mode.
TOOL_CAPABILITIES = {
    "memory_ingest_document": "ingest_document",
    "code_blame": "blame_code",
    "code_read_transcript": "read_transcript_centered",
    "peer_chat": "peer_chat",
}


class BackendCapabilityMiddleware(Middleware):
    """Resolve at listing/call time, preserving lazy backend initialization."""

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        if not any(tool.name in TOOL_CAPABILITIES for tool in tools):
            return tools
        backend = get_backend()
        return [
            tool
            for tool in tools
            if tool.name not in TOOL_CAPABILITIES
            or backend.supports(TOOL_CAPABILITIES[tool.name])
        ]

    async def on_call_tool(self, context, call_next):
        capability = TOOL_CAPABILITIES.get(context.message.name)
        if capability and not get_backend().supports(capability):
            raise ToolError(
                f"{context.message.name} is not supported by the active backend."
            )
        return await call_next(context)
