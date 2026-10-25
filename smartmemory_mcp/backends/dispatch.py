"""Backend dispatch — resolve the remote (REST) or local (in-process) backend.

REST by default (MCP-PERF-1, 2026-10-24). The MCP talks to a SmartMemory service
over HTTP unless the operator opts into the in-process store with
``SMARTMEMORY_MCP_MODE=local``. Having the ``smartmemory`` package importable is
NOT an opt-in: before this change every stdio MCP process that could import it
built a full in-process memory (embedding model, spaCy, reranker), 1.6-4 GB and up
to a minute of CPU per Claude Code session, even with ``SMARTMEMORY_API_URL`` set.

``SMARTMEMORY_MCP_MODE``:
- unset / ``remote`` -> RemoteBackend (URL: ``SMARTMEMORY_API_URL``, else the
  smartmemory config's remote URL, else https://api.smartmemory.ai)
- ``local`` -> LocalBackend (in-process store, loads models on first use)
- ``hosted`` -> the multi-tenant server; handled before this module is consulted
- anything else -> one WARNING, then remote (never loads models)

The smartmemory config file's ``mode = "local"`` is the CLI's setting; the MCP no
longer follows it, and says so once at WARNING.

Result cached in module-level _backend after first resolution.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

from smartmemory_mcp.hosted.identity import resolve_hosted_backend

if TYPE_CHECKING:
    from smartmemory_mcp.backends.interface import MemoryBackend

log = logging.getLogger(__name__)

MODE_ENV = "SMARTMEMORY_MCP_MODE"
MODE_LOCAL = "local"
MODE_REMOTE = "remote"
MODE_HOSTED = "hosted"
_KNOWN_MODES = (MODE_LOCAL, MODE_REMOTE, MODE_HOSTED)
PROBE_TIMEOUT_S = 2.0

_backend: "MemoryBackend | None" = None
_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key in _warned:
        return
    _warned.add(key)
    log.warning(msg, *args)


def requested_mode() -> str:
    """The backend mode this process runs in: ``local``, ``remote`` or ``hosted``."""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    if not raw:
        return MODE_REMOTE
    if raw in _KNOWN_MODES:
        return raw
    _warn_once(
        f"bad-mode:{raw}",
        "%s=%r is not one of local|remote|hosted; using remote (REST only, "
        "no models are loaded in this process).",
        MODE_ENV,
        raw,
    )
    return MODE_REMOTE


def set_process_mode(mode: str, workspace_id: str | None = None) -> None:
    """Select the backend mode for this process and drop the cached backend.

    ``workspace_id`` carries a live remote workspace (an unsaved
    ``switch_workspace``) into the rebuilt remote backend, which is then built
    here rather than on the next call. Without it a rebuild starts from the
    configured default workspace (MCP-SWITCH-WORKSPACE-1 H1).
    """
    global _backend
    os.environ[MODE_ENV] = mode
    reset_backend()
    if workspace_id and mode == MODE_REMOTE:
        _backend = build_backend(MODE_REMOTE, team_id=workspace_id)


def _app_config():
    """The smartmemory CLI config, or None when the package is absent or unreadable."""
    try:
        from smartmemory_app.config import load_config
    except ImportError:
        return None
    try:
        return load_config()
    except Exception as exc:  # invalid SMARTMEMORY_MODE, unreadable TOML
        _warn_once(
            "app-config",
            "smartmemory config unreadable (%s); remote backend uses env/defaults.",
            exc,
        )
        return None


def build_backend(mode: str, team_id: str | None = None) -> "MemoryBackend":
    """Construct (uncached) the backend for ``mode`` (``local`` or ``remote``).

    ``team_id`` (remote only) overrides the configured workspace.
    """
    if mode == MODE_LOCAL:
        log.info("Backend dispatch: local mode (%s=local, in-process store)", MODE_ENV)
        from smartmemory_mcp.backends.local import LocalBackend

        return LocalBackend()

    from smartmemory_mcp.backends.remote import RemoteBackend
    from smartmemory_mcp.tier import get_api_key

    cfg = _app_config()
    if cfg is not None and getattr(cfg, "mode", None) == MODE_LOCAL:
        _warn_once(
            "cfg-local",
            "smartmemory config says mode=local, but the MCP uses the REST backend "
            "unless %s=local is set; no in-process store is opened.",
            MODE_ENV,
        )
    api_key = get_api_key()
    if not api_key and cfg is not None:
        # Same as before for a configured install: an unauthenticated REST backend,
        # so tools/list still works and `whoami` points at `login`.
        _warn_once(
            "no-key",
            "No SmartMemory API key: REST tools will fail until login(api_key) or "
            "SMARTMEMORY_API_KEY; %s=local runs the store in this process instead.",
            MODE_ENV,
        )
    elif not api_key:
        raise RuntimeError(
            "No SmartMemory backend available: no API key for the REST backend.\n"
            "Either:\n"
            "  1. Set SMARTMEMORY_API_KEY (and SMARTMEMORY_API_URL) or run login(api_key)\n"
            f"  2. Set {MODE_ENV}=local to run the store in this process "
            "(needs: pip install smartmemory)"
        )
    api_url = os.environ.get("SMARTMEMORY_API_URL") or getattr(cfg, "api_url", None)
    team_id = team_id or getattr(cfg, "team_id", None) or None
    log.info("Backend dispatch: remote mode")
    return RemoteBackend(api_url=api_url or None, api_key=api_key, team_id=team_id)


def resolve_backend() -> "MemoryBackend":
    """Return the active backend, creating it on first call.

    Hosted first (per-call tenant backend from the middleware), then the cached
    process backend, then ``build_backend(requested_mode())``.
    """
    # Path 0: hosted mode — the middleware already built a per-call, per-tenant
    # backend and bound it to the contextvar. Return THAT object; never rebuild
    # one (a rebuild loses the session-selected team) and never fall through to
    # the process singleton (it would act as the wrong tenant).
    hosted = resolve_hosted_backend()
    if hosted is not None:
        return hosted

    global _backend
    if _backend is not None:
        return _backend

    mode = requested_mode()
    _backend = build_backend(MODE_LOCAL if mode == MODE_LOCAL else MODE_REMOTE)
    return _backend


def remote_api_url() -> str:
    """The URL the REST backend would use, without building it."""
    from smartmemory_mcp.backends.remote import DEFAULT_API_URL

    cfg = _app_config()
    return (
        os.environ.get("SMARTMEMORY_API_URL")
        or getattr(cfg, "api_url", None)
        or DEFAULT_API_URL
    ).rstrip("/")


def check_service_reachable(api_url: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """GET ``<api_url>/health``; any HTTP answer counts. One WARNING if none comes."""
    import httpx

    try:
        httpx.get(f"{api_url}/health", timeout=timeout)
        return True
    except httpx.HTTPError as exc:
        _warn_once(
            f"unreachable:{api_url}",
            "SmartMemory service unreachable at %s (%s): memory tools will return "
            "errors until it answers. The MCP does not fall back to an in-process "
            "store; set %s=local to opt into one.",
            api_url,
            type(exc).__name__,
            MODE_ENV,
        )
        return False


def start_reachability_probe() -> threading.Thread | None:
    """Probe the REST service in the background so a dead URL is logged at boot."""
    if requested_mode() != MODE_REMOTE:
        return None
    url = remote_api_url()
    thread = threading.Thread(
        target=check_service_reachable, args=(url,), name="sm-api-probe", daemon=True
    )
    thread.start()
    return thread


def reset_backend() -> None:
    """Clear the cached backend. Used by tests to force re-resolution."""
    global _backend
    _backend = None
