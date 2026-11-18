"""Per-tool-call trace context: `X-SM-Tool` headers for REST, a core span for local (INSIGHTS-MCP-TRACES-1).

Remote and hosted installs ship without core, so they never produce a span here: the
tool name and a per-call trace id ride on every HTTP request the backend makes
(`RemoteBackend._headers`), and svc-api emits the `mcp.<tool>` event on the caller's
own workspace stream. Local stdio mode (`SMARTMEMORY_MCP_MODE=local`, core in process)
wraps the call in `trace_span("mcp.<tool>")`, which core routes to the progress stream
or the Lite sink. Contract: smart-memory-docs `docs/features/INSIGHTS-MCP-TRACES-1/mcp-span-contract.json`.

Tracing never fails a tool call: a lost span logs one WARNING naming the tool and the reason.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar

from fastmcp.server.middleware import Middleware

logger = logging.getLogger(__name__)

TOOL_HEADER = "X-SM-Tool"
TRACE_ID_HEADER = "X-SM-Trace-Id"
_TOOL_RE = re.compile(r"\w{1,64}", re.ASCII)

_tool: ContextVar[str | None] = ContextVar("sm_mcp_tool", default=None)
_trace_id: ContextVar[str | None] = ContextVar("sm_mcp_trace_id", default=None)


def tool_headers() -> dict[str, str]:
    """The trace headers for the tool call in progress, or `{}` outside one."""
    tool = _tool.get()
    if tool is None:
        return {}
    return {TOOL_HEADER: tool, TRACE_ID_HEADER: _trace_id.get() or ""}


def _warn_lost(tool: str, reason: object) -> None:
    logger.warning("mcp span lost: %s: %s", tool, reason)


def _local_tracing() -> bool:
    """Only the in-process backend traces in core; REST and hosted rely on svc-api."""
    from smartmemory_mcp.backends.dispatch import MODE_ENV, MODE_LOCAL

    if os.environ.get(MODE_ENV, "").strip().lower() != MODE_LOCAL:
        return False
    from smartmemory_mcp.hosted.identity import hosted_mode_enabled

    return not hosted_mode_enabled()


def _open_span(tool: str):
    """Enter `trace_span("mcp.<tool>")`; on any failure WARN and return `(None, None)`."""
    try:
        from smartmemory.observability.tracing import trace_span

        cm = trace_span(f"mcp.{tool}", {"tool": tool, "backend": "local"})
        return cm, cm.__enter__()
    except Exception as exc:
        _warn_lost(tool, type(exc).__name__)
        return None, None


def _close_span(tool: str, cm, span, error: BaseException | None) -> None:
    if cm is None:
        return
    try:
        if error is None:
            cm.__exit__(None, None, None)
        else:
            if span is not None:
                span.attributes["error_type"] = type(error).__name__
            cm.__exit__(type(error), error, error.__traceback__)
    except BaseException as exc:
        if exc is error:
            return  # the tool's own error, re-raised by the span: not a tracing failure
        _warn_lost(tool, type(exc).__name__)


class ToolTraceMiddleware(Middleware):
    """Bind the tool name and a fresh trace id for one tool call; span it in local mode."""

    async def on_call_tool(self, context, call_next):
        tool = context.message.name
        if not _TOOL_RE.fullmatch(tool or ""):
            _warn_lost(tool, "tool name is not \\w{1,64}")
            return await call_next(context)
        tool_token = _tool.set(tool)
        trace_token = _trace_id.set(uuid.uuid4().hex)
        try:
            local = False
            try:
                local = _local_tracing()
            except Exception as exc:
                _warn_lost(tool, type(exc).__name__)
            if not local:
                return await call_next(context)
            cm, span = _open_span(tool)
            try:
                result = await call_next(context)
            except BaseException:
                _close_span(tool, cm, span, sys.exc_info()[1])
                raise
            _close_span(tool, cm, span, None)
            return result
        finally:
            _trace_id.reset(trace_token)
            _tool.reset(tool_token)
