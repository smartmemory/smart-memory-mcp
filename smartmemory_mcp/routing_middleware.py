"""Stdio routing middleware (WS-ROUTING-5): `X-SM-Client` and the write-target line for the remote backend.

Hosted mode does the same inside `hosted.middleware.HostedIdentityMiddleware`. On stdio the process backend is one
object, so each call stamps it with this MCP client's key (`mcp:<clientInfo.name>`, the key the service stores a
session pick and a per-client default under) and opens a per-call list the backend records its write echoes into.
Local (Lite) backends are untouched: Lite has no workspaces.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware.middleware import Middleware
from mcp.types import TextContent

from smartmemory_mcp.backends.remote import (
    WRITE_TARGETS,
    RemoteBackend,
    client_key_from_name,
    client_name_from_context,
    format_write_target,
)
from smartmemory_mcp.tools.common import get_backend

logger = logging.getLogger(__name__)


class RemoteRoutingMiddleware(Middleware):
    """Stamp the remote backend's client key and name each write tool's target workspace."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            backend = get_backend()
        except RuntimeError:
            backend = None
        if not isinstance(backend, RemoteBackend):
            return await call_next(context)
        name = client_name_from_context(context.fastmcp_context)
        if name:
            backend.set_client_key(client_key_from_name(name))
        targets: list[dict[str, Any]] = []
        token = WRITE_TARGETS.set(targets)
        try:
            result = await call_next(context)
        finally:
            WRITE_TARGETS.reset(token)
        if targets and hasattr(result, "content") and not await _read_only(context):
            fallback = None
            if not targets[-1].get("workspace_id"):
                fallback = backend._explain_routing()
            result.content = [
                *result.content,
                TextContent(type="text", text=format_write_target(targets, fallback)),
            ]
        return result


async def _read_only(context: Any) -> bool:
    try:
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
    except Exception:  # noqa: BLE001 - an unknown tool simply gets the line
        return False
    return bool(getattr(getattr(tool, "annotations", None), "readOnlyHint", False))
