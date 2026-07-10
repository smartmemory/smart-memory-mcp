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
import sys

from fastmcp import FastMCP

from smartmemory_mcp.tier import Tier, resolve_tier, get_api_key, store_api_key
from smartmemory_mcp.tools.common import graceful

logger = logging.getLogger(__name__)

mcp = FastMCP("smartmemory")


# ---------------------------------------------------------------------------
# Auth tools — always registered (part of FREE tier)
# ---------------------------------------------------------------------------


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
        )

        decision_tools.register(mcp)
        code_tools.register(mcp)
        graph_tools.register(mcp)
        anchor_tools.register(mcp)
        plan_tools.register(mcp)
        agent_tools.register(mcp)
        structured_tools.register(mcp)
        pattern_tools.register(mcp)

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


# Register tools at module load
_register_tools()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    if "--http" in sys.argv:
        port = 8011
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        mcp.run(transport="http", host="0.0.0.0", port=port, show_banner=False)
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
