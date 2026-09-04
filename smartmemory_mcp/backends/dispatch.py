"""Backend dispatch — resolve local or remote backend based on config.

Local-first: if smartmemory package is installed and mode != "remote", use LocalBackend.
Remote: if mode == "remote" or only env vars available, use RemoteBackend.
Result cached in module-level _backend after first resolution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from smartmemory_mcp.hosted.identity import resolve_hosted_backend

if TYPE_CHECKING:
    from smartmemory_mcp.backends.interface import MemoryBackend

log = logging.getLogger(__name__)

_backend: "MemoryBackend | None" = None


def resolve_backend() -> "MemoryBackend":
    """Return the active backend, creating it on first call.

    Resolution order:
    1. Try importing smartmemory_app.config.load_config — if available, read mode.
       - mode == "remote" -> RemoteBackend with config values
       - mode == "local" or mode is set -> LocalBackend
    2. If smartmemory not installed, check SMARTMEMORY_API_KEY env var -> RemoteBackend.
    3. No backend resolvable -> raise RuntimeError.
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

    # Path 1: smartmemory package installed — use its config system
    try:
        from smartmemory_app.config import load_config

        cfg = load_config()
        if cfg.mode == "remote":
            log.info("Backend dispatch: remote mode (from config)")
            from smartmemory_mcp.backends.remote import RemoteBackend
            from smartmemory_mcp.tier import get_api_key

            _backend = RemoteBackend(
                api_url=cfg.api_url,
                api_key=get_api_key(),  # Reads env var → keyring → file
                team_id=cfg.team_id,
            )
            return _backend

        # Local mode (mode == "local" or mode is set to anything else, or None with package available)
        log.info("Backend dispatch: local mode (smartmemory installed)")
        from smartmemory_mcp.backends.local import LocalBackend

        _backend = LocalBackend()
        return _backend

    except ImportError:
        pass  # smartmemory package not installed — fall through to env var path

    # Path 2: No smartmemory package — check for stored/env API key
    from smartmemory_mcp.tier import get_api_key

    api_key = get_api_key()
    if api_key:
        log.info("Backend dispatch: remote mode (from stored API key)")
        from smartmemory_mcp.backends.remote import RemoteBackend

        _backend = RemoteBackend(api_key=api_key)
        return _backend

    # Path 3: No backend resolvable
    raise RuntimeError(
        "No SmartMemory backend available.\n"
        "Either:\n"
        "  1. Install the full package: pip install smartmemory\n"
        "  2. Set SMARTMEMORY_API_KEY env var for remote mode\n"
        "  3. Run: smartmemory setup"
    )


def reset_backend() -> None:
    """Clear the cached backend. Used by tests to force re-resolution."""
    global _backend
    _backend = None
