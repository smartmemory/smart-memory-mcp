"""Resolve as_of once at the MCP boundary and disclose inferred UTC instants."""

# === 1. IMPORTS ===
from datetime import datetime
from functools import wraps
from inspect import signature
import re

from fastmcp.exceptions import ToolError

# === 2. TYPES & CONSTANTS ===
_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_ISO_DATE = r"[0-9]{4}(?:-?[0-9]{2}-?[0-9]{2}|-?W[0-9]{2}(?:-?[1-7])?)"
_TIME = r"[0-9]{2}(?::?[0-9]{2}(?::?[0-9]{2})?)?(?:[.,][0-9]+)?"
_ZONE = r"(?:Z|[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?)?"
_TIMESTAMP = re.compile(rf"{_ISO_DATE}[Tt ]{_TIME}{_ZONE}")


# === 3. PUBLIC API ===
def with_as_of(fn):
    """Pass a resolved instant to the backend and report defaults on the tool result."""
    params = signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        # 1. Resolve explicit input before the tool reaches its backend.
        bound = params.bind(*args, **kwargs)
        value = bound.arguments.get("as_of")
        if value is None:
            return fn(*args, **kwargs)
        instant, reason = _resolve(value)
        bound.arguments["as_of"] = instant

        # 2. Keep the tool's text or structured result shape.
        if reason is None:
            return fn(*bound.args, **bound.kwargs)
        note = f"as of {instant}, {reason}"
        try:
            result = fn(*bound.args, **bound.kwargs)
        except ToolError as exc:
            raise ToolError(f"{exc}\n{note}") from exc
        except ValueError as exc:
            # Lite explicitly refuses historical policy compilation on an unscoped store.
            if getattr(exc, "code", None) != "invalid_selector":
                raise
            raise ToolError(f"{exc}\n{note}") from exc

        # 3. Include the default even on an empty historical result.
        if isinstance(result, dict):
            return {**result, "as_of_note": note}
        return f"{result}\n{note}"

    wrapped.__doc__ = (fn.__doc__ or "").rstrip() + (
        "\n\nas_of accepts YYYY-MM-DD (end of day UTC at 23:59:59) or an ISO 8601 "
        "timestamp. Times without a zone use UTC. Zoned times keep their offset. "
        "Omit as_of for the current view."
    )
    return wrapped


# === 4. INTERNAL HELPERS ===
def _resolve(value: str) -> tuple[str, str | None]:
    """Validate a calendar date or ISO timestamp and resolve any UTC default."""
    try:
        if re.fullmatch(_DATE, value):
            datetime.fromisoformat(value)
            return f"{value}T23:59:59Z", "date read as end of day UTC"
        if not _TIMESTAMP.fullmatch(value):
            raise ValueError
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ToolError(
            f"Invalid as_of {value!r}. Use a date or timestamp, "
            "for example 2026-03-01 or 2026-03-01T09:12:00+08:00."
        ) from exc
    if stamp.tzinfo is None:
        return stamp.isoformat() + "Z", "time read as UTC"
    return value, None
