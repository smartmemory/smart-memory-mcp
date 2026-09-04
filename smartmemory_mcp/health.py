"""Shared unauthenticated liveness routes for SmartMemory MCP servers."""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse


def register_health(mcp: FastMCP, *, mode: str) -> None:
    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> JSONResponse:
        """Unauthenticated liveness probe for the container healthcheck."""
        return JSONResponse({"status": "ok", "mode": mode})
