"""SmartMemory Unified MCP Server.

Tiered tool registration:
  FREE  (31 tools) — no login required, includes the decision suite
  PRO   (69 tools) — after smartmemory login
  PRO+  (100 tools) — PRO + SMARTMEMORY_MCP_FULL_TOOLS=true
(Counts are cumulative registered tools, asserted by tests/test_tier_registration.py.)

Backend is independent of tier:
  Local  — default, uses smartmemory package (pip install smartmemory)
  Remote — explicit opt-in via smartmemory setup --mode remote
"""

import logging
import os
import sys

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from smartmemory_mcp.backends.remote import (
    LEGACY_TEAM_ENV,
    WORKSPACE_ENV,
    env_workspace,
)
from smartmemory_mcp.capabilities import BackendCapabilityMiddleware
from smartmemory_mcp.routing_middleware import RemoteRoutingMiddleware
from smartmemory_mcp.health import register_health
from smartmemory_mcp.instructions import LOCAL_INSTRUCTIONS
from smartmemory_mcp.tier import Tier, resolve_tier, store_api_key
from smartmemory_mcp.tools.common import commit_if_current, get_backend, graceful

logger = logging.getLogger(__name__)

mcp = FastMCP("smartmemory", instructions=LOCAL_INSTRUCTIONS)
mcp.add_middleware(BackendCapabilityMiddleware())
mcp.add_middleware(RemoteRoutingMiddleware())

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
    """Show current session: user, tenant, workspace, tier, and backend mode."""
    # Core is optional for remote-only MCP installs.
    try:
        from smartmemory.errors import MissingModelError
    except ImportError:
        missing_model_errors = ()
    else:
        missing_model_errors = (MissingModelError,)

    tier = resolve_tier()
    lines = [f"Tier: {tier.name}"]

    # Report the ACTUAL resolved backend — constructing a throwaway
    # RemoteBackend(api_key=...) here printed the env-default api_url
    # (api.smartmemory.ai) and an empty workspace even when the real backend was
    # configured against a different service, and duplicated the "Backend:"
    # line the remote whoami already emits.
    try:
        # The same object every memory tool uses (`tools.common.get_backend`), so
        # this shows the workspace a `switch_workspace` just selected.
        backend = get_backend()
        lines.append(backend.whoami())
    except missing_model_errors as exc:
        logger.warning("Local model unavailable: %s", exc)
        lines.append(str(exc))
    except RuntimeError:
        lines.append("Not logged in. Run login(api_key) to authenticate.")
        lines.append("Backend: none resolved")

    return "\n".join(lines)


_SWITCH_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _switch_workspace(workspace: str, save_as_default: bool = False) -> str:
    """Switch the live session every tool uses (one behaviour, no restart).

    `get_backend()` returns the process backend the memory tools call, which is
    the one `backends/dispatch.py` built and caches; mutating its session makes
    the next request from any tool carry the new `X-Workspace-Id`. The name is
    resolved over the network first, then committed under `rebuild_lock` only if
    that object is still the process backend: a `memory_migrate` rebuild in
    between would otherwise take the write onto an orphan and leave a false
    "Switched" reply (MCP-SWITCH-INFLIGHT-REBUILD-1).
    """
    from smartmemory_mcp.workspace_select import refuse

    try:
        backend = get_backend()
    except RuntimeError as exc:
        return refuse(f"no backend is available ({exc}).", "").message
    result = backend.resolve_switch(workspace)
    if not result.ok:
        return result.message
    if not commit_if_current(
        backend, lambda: backend.set_active_workspace(result.workspace_id)
    ):
        return refuse(
            "memory_migrate replaced the backend while this switch was looking up "
            "your workspaces, so it was not applied. Run switch_workspace again.",
            _active_workspace_label(),
        ).message
    if not save_as_default:
        return result.message
    return f"{result.message}\n{_save_default_workspace(result.workspace_id)}"


def _active_workspace_label() -> str:
    """The workspace the current process backend's next request uses, for a refusal."""
    from smartmemory_mcp.backends.local import LOCAL_WORKSPACE, LocalBackend

    try:
        current = get_backend()
    except RuntimeError:
        return ""
    if isinstance(current, LocalBackend):
        return f"{LOCAL_WORKSPACE} (local mode)"
    workspace = getattr(current, "active_workspace_id", "")
    return workspace if isinstance(workspace, str) else ""


def _save_default_workspace(workspace_id: str) -> str:
    """Write the workspace into the smartmemory config for new sessions.

    A new session starts on `SMARTMEMORY_WORKSPACE_ID`, else `SMARTMEMORY_TEAM_ID`,
    else this saved value (`remote.env_workspace`), so the reply says when an env
    var will win over what was just saved (MCP-WORKSPACE-ENV-PRECEDENCE-1).
    """
    try:
        from smartmemory_app.config import load_config, save_config
    except ImportError:
        logger.warning(
            "Default workspace not saved: smartmemory package not installed."
        )
        return (
            "Not saved as the default: the smartmemory package is not installed. "
            f"Set {WORKSPACE_ENV}={workspace_id} for new sessions instead "
            f"(it overrides the older {LEGACY_TEAM_ENV})."
        )
    from smartmemory_mcp.backends.dispatch import file_layer_config

    cfg = file_layer_config(load_config)  # never persist the shell's env values
    cfg.team_id = workspace_id  # config key name kept; CLI rename is a follow-up
    save_config(cfg)
    env_value, env_name = env_workspace()
    if env_value and env_value != workspace_id:
        return (
            f"Saved as the default, but new sessions start on {env_value}: "
            f"{env_name} is set, and an env var wins over the saved default. "
            f"Unset {env_name} to use the saved one."
        )
    return "Saved as the default for new sessions."


@mcp.tool(title="Switch workspace", annotations=_SWITCH_ANNOTATIONS)
@graceful
def switch_workspace(workspace: str, save_as_default: bool = False) -> str:
    """Switch this session to another workspace you belong to, by id or name.

    Takes effect on the next request; the server refuses a workspace you are not
    a member of. On failure the previous workspace stays active. Set
    `save_as_default` to also use it for new sessions.
    """
    return _switch_workspace(workspace, save_as_default)


@mcp.tool(
    title="List workspaces",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@graceful
def list_workspaces() -> str:
    """List the workspaces you can switch to; the current one is marked."""
    from smartmemory_mcp.backends.local import LocalBackend
    from smartmemory_mcp.workspace_select import format_workspace_rows

    backend = get_backend()
    if isinstance(backend, LocalBackend):
        return (
            "Lite (local mode) has no workspaces: everything is in one local store. "
            "Use remote mode (smartmemory setup --mode remote) for workspaces."
        )
    rows, why = backend._fetch_workspaces()
    if rows is None:
        return f"Could not list your workspaces: {why}"
    return format_workspace_rows(rows, backend.active_workspace_id)


@mcp.tool(title="Switch team (deprecated)", annotations=_SWITCH_ANNOTATIONS)
@graceful
def switch_team(team_id: str) -> str:
    """Deprecated alias of switch_workspace; removed next release. team_id is a workspace id or name."""
    from smartmemory_mcp.workspace_select import ALIAS_NOTICE, warn_alias_once

    warn_alias_once()
    return f"{ALIAS_NOTICE}\n{_switch_workspace(team_id)}"


# ---------------------------------------------------------------------------
# Tool registration by tier
# ---------------------------------------------------------------------------


def _register_tools():
    """Register tools based on resolved tier."""
    tier = resolve_tier()
    logger.info("SmartMemory MCP starting with tier: %s", tier.name)

    # FREE tier (always registered)
    from smartmemory_mcp.tools import (
        decision_tools,
        memory_tools,
        portability_tools,
        lifecycle_tools,
    )

    memory_tools.register_free(mcp)
    memory_tools.register_feedback(mcp)
    portability_tools.register(mcp)
    lifecycle_tools.register(mcp)
    # DEV-ONBOARD-FU-MCP-DECISION-TOOLS-FREE-1: decision tools are FREE, matching the
    # Python SDK where add_decision needs no key. Only this suite moved; the rest of
    # the PRO block below is unchanged.
    decision_tools.register(mcp)

    # PRO tier
    if tier >= Tier.PRO:
        memory_tools.register_pro(mcp)

        from smartmemory_mcp.tools import (
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
    # Only on the explicit in-process opt-in (MCP-PERF-1): in the default REST mode a
    # boot warm-up loaded ~600 MB of models into every session that never searched.
    from smartmemory_mcp.backends import dispatch

    if (
        _TRANSCRIPT_TOOLS_REGISTERED
        and dispatch.requested_mode() == dispatch.MODE_LOCAL
    ):
        from smartmemory_mcp.tools import transcript_tools

        transcript_tools.schedule_warm_start()
    dispatch.start_reachability_probe()

    if "--http" in sys.argv:
        _register_http_health(mcp)
        host, port = resolve_http_bind()
        mcp.run(transport="http", host=host, port=port, show_banner=False)
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
