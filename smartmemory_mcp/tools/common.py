"""Shared utilities for unified MCP tool modules."""

import functools
import logging
from typing import Any, Callable

from smartmemory_mcp.backends.dispatch import rebuild_lock, resolve_backend
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
    with rebuild_lock:
        if _backend is None:
            _backend = resolve_backend()
        return _backend


def commit_if_current(backend: Any, apply: Callable[[], None]) -> bool:
    """Run ``apply()`` under ``rebuild_lock`` only while ``backend`` is still the one
    every tool gets; return whether it ran.

    A tool that took ``get_backend()`` and then waited on the network can come back
    after ``memory_migrate`` rebuilt the process backend; writing onto the old
    object then changes nothing anyone uses (MCP-SWITCH-INFLIGHT-REBUILD-1). The
    rebuild clears this cache under the same lock, so the check and the write are
    atomic against it.
    """
    with rebuild_lock:
        if _backend is not backend:
            return False
        apply()
        return True


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


def checked_as_of(as_of):
    """The ``as_of`` argument of a dated read (SM-ASOF-DECISIONS-1), checked before any backend call.

    None stays None (now). Anything else must be an ISO 8601 instant; the original string is passed on unchanged
    (core and the service own the as-of semantics). An unparseable value is a ``ToolError`` naming it, never a
    silent "now".
    """
    if as_of is None:
        return None
    from datetime import datetime

    from fastmcp.exceptions import ToolError

    try:
        datetime.fromisoformat(str(as_of).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError(f"as_of is not an ISO 8601 timestamp: {as_of!r}") from exc
    return as_of


def dated(as_of) -> dict:
    """``{"as_of": as_of}`` when set, else ``{}``: a call without it stays byte-identical."""
    return {} if as_of is None else {"as_of": as_of}


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
