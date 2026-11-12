"""Shared utilities for unified MCP tool modules."""

import json
import functools
import logging
from typing import Any, Callable

from smartmemory_mcp.backends.dispatch import rebuild_lock, resolve_backend
from smartmemory_mcp.hosted.identity import resolve_hosted_backend

logger = logging.getLogger(__name__)


def metadata_dict(item: Any, tool: str = "") -> dict:
    """``item["metadata"]`` as a dict. Truthy non-dict metadata (str, list, int)
    gets one WARNING and reads as empty: the line that needs it is left out and
    the call never fails (MCP-MEMGET-META-1 design, 8de1eee/60f70fb)."""
    md = item.get("metadata") if isinstance(item, dict) else None
    if not md:
        return {}
    if isinstance(md, dict):
        return md
    logger.warning(
        "%s item %s has %s metadata, not a dict; its metadata was not read",
        tool or "read tool",
        item.get("item_id"),
        type(md).__name__,
    )
    return {}


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


def target_workspace(backend: Any) -> str:
    """The workspace a write through ``backend`` lands in: name when known, else id (WS-ROUTING-5).

    Read before the write is sent, so the label is the one that call's ``X-Workspace-Id`` carries.
    """
    label = getattr(backend, "workspace_label", None)
    if callable(label):
        return str(label())
    from smartmemory_mcp.backends.local import LOCAL_WORKSPACE

    return f"{LOCAL_WORKSPACE} (local mode)"


def name_workspace(result: Any, workspace: str) -> Any:
    """Add the target workspace to a write tool's result: a text line, and a ``workspace`` field when structured."""
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent

    line = f"Workspace: {workspace}"
    if isinstance(result, ToolResult):
        result.content = [*result.content, TextContent(type="text", text=line)]
        if isinstance(result.structured_content, dict):
            result.structured_content = {
                **result.structured_content,
                "workspace": workspace,
            }
        return result
    if isinstance(result, dict):
        return {**result, "workspace": workspace}
    return f"{result}\n{line}"


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
            if type(e).__name__ == "RecallConflictError":
                from fastmcp.exceptions import ToolError

                raise ToolError("recall_conflict: " + str(e)) from e
            code = getattr(e, "code", None)
            if code in {
                "invalid_scoped_validity",
                "scoped_validity_conflict",
                "scoped_history_unavailable",
            }:
                from fastmcp.exceptions import ToolError

                raise ToolError(f"{code}: {e}") from e
            if _is_connection_error(e):
                return (
                    "SmartMemory backend not reachable. Check that:\n"
                    "  - Local mode: smartmemory daemon is running (smartmemory start)\n"
                    "  - Remote mode: API is reachable and API key is valid"
                )
            raise

    return wrapper


def scoped_options(
    *, business_scope=None, valid_at=None, scope_go_lives=None, valid_from=None
):
    """Validate U01 inputs without core installed, copy them and omit absent options.

    History conflicts and request-start defaults belong to core/service. This shared
    boundary validates only the portable wire values, never ownership or history.
    """
    from copy import deepcopy
    from datetime import datetime, timezone

    from fastmcp.exceptions import ToolError

    def refuse(message):
        raise ToolError(f"invalid_scoped_validity: {message}")

    def scope(value):
        if not isinstance(value, dict) or set(value) != {"site"}:
            refuse("business_scope requires only site")
        site = value["site"]
        if (
            not isinstance(site, str)
            or not site.strip()
            or len(site) > 128
            or "*" in site
            or "?" in site
        ):
            refuse("invalid exact site")
        return dict(value)

    def instant(value):
        try:
            if isinstance(value, datetime):
                parsed = value
            else:
                if not isinstance(value, str) or not any(
                    c in value for c in ("T", " ")
                ):
                    refuse("validity date needs a datetime")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str):
                return value  # validated, backend owns UTC normalization
            return (
                parsed.replace(tzinfo=timezone.utc).isoformat()
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc).isoformat()
            )
        except (ValueError, TypeError) as exc:
            raise ToolError(
                "invalid_scoped_validity: invalid validity datetime"
            ) from exc

    options = {}
    if business_scope is not None:
        options["business_scope"] = scope(business_scope)
    if valid_at is not None:
        options["valid_at"] = instant(valid_at)
    if valid_from is not None:
        options["valid_from"] = instant(valid_from)
    if scope_go_lives is not None:
        if valid_from is not None:
            refuse("valid_from and scope_go_lives are incompatible")
        if not isinstance(scope_go_lives, list) or not 1 <= len(scope_go_lives) <= 256:
            refuse("scope_go_lives requires 1 to 256 entries")
        entries, seen = [], set()
        for entry in scope_go_lives:
            if not isinstance(entry, dict) or set(entry) != {"scope", "valid_from"}:
                refuse("schedule entries require scope and valid_from only")
            qualifier = None if entry["scope"] is None else scope(entry["scope"])
            key = None if qualifier is None else qualifier["site"]
            if key in seen:
                refuse("duplicate schedule scope")
            seen.add(key)
            entries.append(
                {"scope": qualifier, "valid_from": instant(entry["valid_from"])}
            )
        options["scope_go_lives"] = entries
    return deepcopy(options)


def refuse_scoped_options(business_scope, valid_at):
    """Unsupported read surfaces must reject supplied qualifiers before backend work."""
    if business_scope is not None or valid_at is not None:
        from fastmcp.exceptions import ToolError

        raise ToolError(
            "invalid_scoped_validity: business_scope and valid_at are unsupported on this tool"
        )


def resolution_text(value):
    """Render only response diagnostics, identically for all backends."""
    if not isinstance(value, dict):
        return ""
    resolution = value.get("scoped_validity_resolution") or metadata_dict(
        value, "resolution_text"
    ).get("scoped_validity_resolution")
    if resolution is not None:
        lines = [f"scoped_validity_resolution: {resolution}"]
    else:
        label = value.get("as_of_resolution")
        lines = [] if label is None else [f"as_of_resolution: {label}"]
    # RECALL-CONFLICT-FLAG-1 D-0039 r1 (M2): conflict evidence renders with every other diagnostic.
    if conflict := value.get("conflict"):
        lines.append("conflict: " + json.dumps(conflict, sort_keys=True))
    return "\n".join(lines)
