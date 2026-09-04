"""The hosted MCP server (S5, design.md §4).

`build_hosted_server` assembles the whole thing: the auth chain, the identity
middleware, the allowlisted tool surface, and the hosted-only session tools.
There is exactly ONE served ASGI object, `hosted_asgi_app`, so the rate-limit
tests drive the same app uvicorn does (round 2 finding 2 / round 3 must-fix 2).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from key_value.aio.protocols.key_value import AsyncKeyValue

from ..health import register_health
from ..tools.common import get_backend, graceful
from .auth import build_auth
from .config import HostedConfig
from .exchange import DEFAULT_EXCHANGE_CACHE, ExchangeCache
from .identity import current_identity, set_hosted_mode
from .middleware import HostedIdentityMiddleware
from .ratelimit import hosted_asgi_middleware
from .tools import apply_allowlist, register_module_tools

logger = logging.getLogger(__name__)


SERVER_NAME = "smartmemory"

HOSTED_REFUSAL = "{what} is not available on the hosted server."


def build_hosted_server(
    cfg: HostedConfig,
    *,
    redis_store: AsyncKeyValue | None = None,
    exchange_cache: ExchangeCache | None = None,
    api_transport: Any = None,
) -> FastMCP:
    """Build the hosted FastMCP server. Injection points exist for tests only."""
    cache = DEFAULT_EXCHANGE_CACHE if exchange_cache is None else exchange_cache
    auth = build_auth(cfg, redis_store=redis_store)
    if api_transport is not None:
        auth.verifiers[0]._transport = api_transport

    identity_middleware = HostedIdentityMiddleware(
        cfg, cache=cache, transport=api_transport
    )
    mcp = FastMCP(
        SERVER_NAME,
        auth=auth,
        middleware=[identity_middleware],
        # The hosted variants of memory_search and memory_recall deliberately
        # supersede the ones their module registers. "replace" is fastmcp's
        # supported route for that (`local_provider.py:186-200`); there is no
        # per-decorator override.
        on_duplicate="replace",
    )

    # From here on, an unidentified tool call is a hard failure rather than a
    # fall-through to the process singleton or to environment credentials.
    set_hosted_mode(True)

    originals = register_module_tools(mcp)
    _register_hosted_tools(mcp, originals, identity_middleware)
    register_health(mcp, mode="hosted")
    apply_allowlist(mcp)
    return mcp


def _register_hosted_tools(
    mcp: FastMCP,
    originals: dict[str, Any],
    identity_middleware: HostedIdentityMiddleware,
) -> None:
    """Session tools and the two guarded overrides."""

    @mcp.tool(name="whoami")
    async def hosted_whoami(ctx: Context) -> str:
        """Show the current hosted session: user, workspace, and auth kind."""
        identity = current_identity.get()
        if identity is None:
            raise ToolError("Unauthorized: no identity bound to this call")
        verified = identity.verified
        team_id = await identity_middleware.get_session_team(ctx) or identity.team_id
        return "\n".join(
            [
                f"User: {verified.email or verified.user_id}",
                f"User ID: {verified.user_id}",
                f"Tenant: {verified.tenant_id}",
                f"Workspace: {team_id}",
                f"Auth: {verified.kind}",
                "Backend: hosted (mcp.smartmemory.ai)",
            ]
        )

    @mcp.tool(name="switch_team")
    async def hosted_switch_team(team_id: str, ctx: Context) -> str:
        """Switch this session to another workspace you belong to.

        The selection is per MCP session, not per process: it lasts as long as
        the connection and never leaks into another user's calls.
        """
        if not team_id or not team_id.strip():
            raise ToolError("team_id is required.")
        team_id = team_id.strip()

        available = _list_team_ids()
        if available is None:
            raise ToolError(
                "Could not list your workspaces; the SmartMemory API did not respond."
            )
        if team_id not in available:
            raise ToolError(
                f"You are not a member of workspace {team_id}. "
                f"Available: {', '.join(sorted(available)) or 'none'}."
            )

        await identity_middleware.set_session_team(ctx, team_id)
        return f"Switched to workspace: {team_id}"

    original_search = originals["memory_search"]

    @mcp.tool(name="memory_search")
    def hosted_memory_search(
        query: str,
        top_k: int = 5,
        memory_type: Optional[str] = None,
        enable_hybrid: bool = True,
        catalog_mode: bool = True,
        decompose: bool = False,
        channel_weights: Optional[dict] = None,
        multi_hop: bool = False,
        max_hops: int = 3,
        budget_ms: int = 1500,
        cite: bool = False,
        as_of_date: Optional[str] = None,
        as_of_strict: bool = False,
        include_superseded: bool = False,
        include_retracted: bool = False,
        include_archived: bool = False,
    ):
        """Search memories using semantic similarity with optional hybrid mode.

        Hosted note: `cite=True` is not available — citation formatting runs in
        the `smartmemory` core package, which the hosted server does not ship.
        """
        if cite:
            # Refused explicitly rather than silently ignored: the caller asked
            # for citations and must know it did not get them (round 3, M5).
            raise ToolError(HOSTED_REFUSAL.format(what="cite=True"))
        return original_search(
            query=query,
            top_k=top_k,
            memory_type=memory_type,
            enable_hybrid=enable_hybrid,
            catalog_mode=catalog_mode,
            decompose=decompose,
            channel_weights=channel_weights,
            multi_hop=multi_hop,
            max_hops=max_hops,
            budget_ms=budget_ms,
            cite=False,
            as_of_date=as_of_date,
            as_of_strict=as_of_strict,
            include_superseded=include_superseded,
            include_retracted=include_retracted,
            include_archived=include_archived,
        )

    @mcp.tool(name="memory_recall")
    @graceful
    def hosted_memory_recall(
        query: str,
        session_id: Optional[str] = None,
        top_k: int = 5,
        cite: bool = False,
    ):
        """Recall recent and relevant memories for the current context.

        Hosted note: this always takes the remote recall path. The alternative
        branch builds a working context through the `smartmemory` core
        activation scorer (round 4, note 2), which the hosted server does not
        ship — so `session_id` and `cite=True` are refused rather than silently
        downgraded.
        """
        if session_id:
            raise ToolError(HOSTED_REFUSAL.format(what="session_id"))
        if cite:
            raise ToolError(HOSTED_REFUSAL.format(what="cite=True"))
        if top_k < 1:
            return "top_k must be at least 1."
        return get_backend().recall(cwd=query, top_k=top_k)


def _list_team_ids() -> set[str] | None:
    """Workspace ids the caller belongs to, or None when the API did not answer."""
    result = get_backend().request("GET", "/memory/teams")
    if isinstance(result, dict) and result.get("error"):
        logger.warning("Workspace lookup failed: %s", result["error"])
        return None
    rows = result.get("teams") if isinstance(result, dict) else result
    if not isinstance(rows, list):
        logger.warning("Workspace lookup returned an unexpected shape: %r", type(rows))
        return None
    ids = {
        str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")
    }
    return ids


def hosted_asgi_app(cfg: HostedConfig, **injection: Any):
    """THE served app. `run_hosted` and every test drive this same object."""
    mcp = build_hosted_server(cfg, **injection)
    return mcp.http_app(
        middleware=hosted_asgi_middleware(cfg),
        stateless_http=False,
    )


def run_hosted(cfg: HostedConfig) -> None:  # pragma: no cover - process entry point
    """Serve the hosted app with uvicorn directly (not `mcp.run()`).

    `mcp.run()` would build its own app and the ASGI rate-limit middleware would
    never be attached to what is actually served.
    """
    import uvicorn

    logger.info(
        "Starting hosted SmartMemory MCP on port %s (public base %s).",
        cfg.hosted_port,
        cfg.public_base_url,
    )
    uvicorn.run(hosted_asgi_app(cfg), host="0.0.0.0", port=cfg.hosted_port)
