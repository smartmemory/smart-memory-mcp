"""Shared utilities for unified MCP tool modules."""

import functools
import logging
from typing import Any

from smartmemory_mcp.backends.dispatch import resolve_backend
from smartmemory_mcp.hosted.identity import resolve_hosted_backend

logger = logging.getLogger(__name__)

_backend = None


def get_backend() -> Any:
    """Return the backend for this call.

    Hosted mode short-circuits the module singleton: the middleware bound a
    per-call, per-tenant backend to the contextvar and that instance is what
    every tool must use. Local/stdio behaviour below is unchanged.
    """
    hosted = resolve_hosted_backend()
    if hosted is not None:
        return hosted

    global _backend
    if _backend is None:
        _backend = resolve_backend()
    return _backend


def reset_backend() -> None:
    """Clear cached backend (for testing)."""
    global _backend
    _backend = None


def _is_connection_error(exc: Exception) -> bool:
    """Detect infrastructure-related connection failures."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "connection refused",
            "no route to host",
            "timed out",
            "connect error",
        )
    )


def graceful(func):
    """Wrap MCP tool to return helpful message on infrastructure failure."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except NotImplementedError as e:
            return str(e)
        except (ImportError, ModuleNotFoundError) as e:
            return (
                f"This tool requires the smartmemory package (not installed).\n"
                f"Install with: pip install smartmemory\n"
                f"Detail: {e}"
            )
        except Exception as e:
            if _is_connection_error(e):
                return (
                    "SmartMemory backend not reachable. Check that:\n"
                    "  - Local mode: smartmemory daemon is running (smartmemory start)\n"
                    "  - Remote mode: API is reachable and API key is valid"
                )
            raise

    return wrapper
