"""SmartMemory Unified MCP Server.

Tiered tool registration:
  FREE  (13 tools) — no login required
  PRO   (58 tools) — after smartmemory login
  PRO+  (89 tools) — PRO + SMARTMEMORY_MCP_FULL_TOOLS=true

Backend is independent of tier:
  Local  — default, uses smartmemory package (pip install smartmemory)
  Remote — explicit opt-in via smartmemory setup --mode remote
"""

import logging
import os
import sys

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from smartmemory_mcp.capabilities import BackendCapabilityMiddleware
from smartmemory_mcp.health import register_health
from smartmemory_mcp.tier import Tier, resolve_tier, store_api_key
from smartmemory_mcp.tools.common import graceful

logger = logging.getLogger(__name__)

mcp = FastMCP("smartmemory")
mcp.add_middleware(BackendCapabilityMiddleware())

# Set when the PRO-tier transcript tools register, so `main()` knows whether the
# search-model warm-up is worth scheduling (FREE tier has no transcript_search).
_TRANSCRIPT_TOOLS_REGISTERED = False


# ---------------------------------------------------------------------------
# Auth tools — always registered (part of FREE tier)
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Log in with an API key",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@graceful
def login(api_key: str) -> str:
    """Authenticate with a SmartMemory API key. Restart MCP server to unlock PRO tools."""
    from smartmemory_mcp.backends.remote import RemoteBackend

    # Validate key with the API
    temp = RemoteBackend(api_key=api_key)
    result = temp.login(api_key)

    if "error" in result.lower() if isinstance(result, str) else False:
        return result

    # Store key for future sessions
    store_api_key(api_key)
    return f"{result}\nRestart MCP server to unlock PRO tools."


@mcp.tool(
    title="Show the current account",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@graceful
def whoami() -> str:
    """Show current session: user, team, tier, and backend mode."""
    from smartmemory_mcp.backends.dispatch import resolve_backend

    tier = resolve_tier()
    lines = [f"Tier: {tier.name}"]

    # Report the ACTUAL resolved backend — constructing a throwaway
    # RemoteBackend(api_key=...) here printed the env-default api_url
    # (api.smartmemory.ai) and an empty team even when the real backend was
    # configured against a different service, and duplicated the "Backend:"
    # line the remote whoami already emits.
    try:
        backend = resolve_backend()
        lines.append(backend.whoami())
    except RuntimeError:
        lines.append("Not logged in. Run login(api_key) to authenticate.")
        lines.append("Backend: none resolved")

    return "\n".join(lines)


@mcp.tool(
    title="Switch workspace",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@graceful
def switch_team(team_id: str) -> str:
    """Switch to a different workspace/team."""
    try:
        from smartmemory_app.config import load_config, save_config

        cfg = load_config()
        cfg.team_id = team_id
        save_config(cfg)
        return f"Switched to team: {team_id}. Restart MCP server to apply."
    except ImportError:
        return f"Team switching requires smartmemory package. Set SMARTMEMORY_TEAM_ID={team_id} env var instead."


# ---------------------------------------------------------------------------
# Tool registration by tier
# ---------------------------------------------------------------------------


def _register_tools():
    """Register tools based on resolved tier."""
    tier = resolve_tier()
    logger.info("SmartMemory MCP starting with tier: %s", tier.name)

    # FREE tier (always registered)
    from smartmemory_mcp.tools import memory_tools, portability_tools, lifecycle_tools

    memory_tools.register_free(mcp)
    memory_tools.register_feedback(mcp)
    portability_tools.register(mcp)
    lifecycle_tools.register(mcp)

    # PRO tier
    if tier >= Tier.PRO:
        memory_tools.register_pro(mcp)

        from smartmemory_mcp.tools import (
            decision_tools,
            code_tools,
            graph_tools,
            anchor_tools,
            plan_tools,
            agent_tools,
            structured_tools,
            pattern_tools,
            peer_tools,
            transcript_tools,
        )

        decision_tools.register(mcp)
        code_tools.register(mcp)
        graph_tools.register(mcp)
        anchor_tools.register(mcp)
        plan_tools.register(mcp)
        agent_tools.register(mcp)
        structured_tools.register(mcp)
        pattern_tools.register(mcp)
        peer_tools.register(mcp)
        transcript_tools.register(mcp)
        global _TRANSCRIPT_TOOLS_REGISTERED
        _TRANSCRIPT_TOOLS_REGISTERED = True

    # PRO+ tier
    if tier >= Tier.PRO_PLUS:
        from smartmemory_mcp.tools import (
            evolution_tools,
            reasoning_tools,
            insight_tools,
            dev_tools,
            zettel_tools,
        )

        evolution_tools.register(mcp)
        reasoning_tools.register(mcp)
        insight_tools.register(mcp)
        dev_tools.register(mcp)
        zettel_tools.register(mcp)


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

HOSTED_LOOPBACK_HOST = "127.0.0.1"
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8011
ALLOW_UNAUTH_HTTP_ENV = "SMARTMEMORY_MCP_ALLOW_UNAUTH_HTTP"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def hosted_mode_requested(argv: list[str] | None = None) -> bool:
    """True when this process should serve the hosted, multi-tenant endpoint."""
    args = sys.argv if argv is None else argv
    if "--hosted" in args:
        return True
    return os.environ.get("SMARTMEMORY_MCP_MODE", "").strip().lower() == "hosted"


def _flag_value(name: str, default: str, argv: list[str]) -> str:
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
    return default


def resolve_http_bind(argv: list[str] | None = None) -> tuple[str, int]:
    """Host and port for `--http`, refusing to expose an unauthenticated server.

    `--http` is the single-identity mode used by the Maya sidecar and local
    experiments: it has NO per-request authentication, so every caller acts as
    whoever the process's API key belongs to. Binding that to 0.0.0.0 publishes
    one tenant's memory to the network. Hosted mode (`--hosted`) is the
    authenticated multi-tenant server; this flag is not it.
    """
    args = sys.argv if argv is None else argv
    requested_host = _flag_value("--host", DEFAULT_HTTP_HOST, args)
    port = int(_flag_value("--port", str(DEFAULT_HTTP_PORT), args))

    allowed = os.environ.get(ALLOW_UNAUTH_HTTP_ENV, "").strip().lower() in _TRUE_VALUES
    is_loopback = requested_host in {HOSTED_LOOPBACK_HOST, "localhost", "::1"}
    if allowed or is_loopback:
        return requested_host, port

    logger.warning(
        "unauthenticated single-identity HTTP mode bound to loopback only; "
        "set %s=true to expose. Requested host %s was replaced with %s: every "
        "caller of --http acts as this process's single API key, with no "
        "per-request authentication. Use --hosted for the authenticated "
        "multi-tenant server.",
        ALLOW_UNAUTH_HTTP_ENV,
        requested_host,
        HOSTED_LOOPBACK_HOST,
    )
    return HOSTED_LOOPBACK_HOST, port


def _register_http_health(mcp: FastMCP, argv: list[str] | None = None) -> None:
    """Register liveness only for the single-identity HTTP transport."""
    args = sys.argv if argv is None else argv
    if "--http" in args:
        register_health(mcp, mode="http")


# Register tools at module load — but NOT in hosted mode, where the tool surface
# is an explicit allowlist built by `hosted.server.build_hosted_server` and tier
# resolution (which reads a stored API key) must never run.
if not hosted_mode_requested():
    _register_tools()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    if hosted_mode_requested():
        from smartmemory_mcp.hosted.config import HostedConfig
        from smartmemory_mcp.hosted.server import run_hosted

        run_hosted(HostedConfig.from_env())
        return

    # Warm the search models before serving. `transcript_search` reranks, and a cold
    # cross-encoder returns UNRANKED results for the first query of the process — which
    # for an MCP server is the first search of the session. Non-blocking, gated on a
    # transcript store actually existing; see transcript_tools.schedule_warm_start.
    if _TRANSCRIPT_TOOLS_REGISTERED:
        from smartmemory_mcp.tools import transcript_tools

        transcript_tools.schedule_warm_start()

    if "--http" in sys.argv:
        _register_http_health(mcp)
        host, port = resolve_http_bind()
        mcp.run(transport="http", host=host, port=port, show_banner=False)
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
