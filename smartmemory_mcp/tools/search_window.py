"""Resolve agent-friendly windows once and echo them on every tool return path."""

import functools
import inspect
import json
from typing import Any, Callable
from datetime import datetime, timedelta, timezone
import re


def resolve_search_window(
    since: str | None = None, until: str | None = None, *, relative: bool = True
) -> dict:
    """Remote MCP installations do not require the core package."""
    now = datetime.now(timezone.utc)
    window = {}
    for name, value in (("since", since), ("until", until)):
        if value is None:
            continue
        try:
            match = re.fullmatch(r"(\d+)([dhm])", value) if relative else None
            stamp = (
                now
                - timedelta(
                    seconds=int(match[1]) * {"d": 86400, "h": 3600, "m": 60}[match[2]]
                )
                if match
                else datetime.fromisoformat(value.replace("Z", "+00:00"))
            )
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            window[name] = stamp.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(
                f"Invalid {name}: {value!r}; expected ISO-8601 or 7d, 24h, 30m"
            ) from exc
    if "since" in window and "until" in window and window["since"] > window["until"]:
        raise ValueError(f"since {since!r} must be <= until {until!r}")
    return window


def search_window_coverage(**window: str) -> dict:
    """No deployment backfill boundary is established yet (contract permits null)."""
    return (
        {
            "created_at_backfilled_boundary": None,
            "note": "Legacy rows may carry first-update time rather than creation time "
            "(CORE-CREATEDAT-BACKFILL-1); results may be over- or under-inclusive.",
        }
        if window
        else {}
    )


def with_search_window(fn: Callable) -> Callable:
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        window = resolve_search_window(
            bound.arguments.get("since"), bound.arguments.get("until"), relative=True
        )
        bound.arguments.update(window)
        strategy = bound.arguments.get("hop_strategy")
        if strategy is not None and strategy not in (
            "consensus",
            "relevance",
            "semantic",
        ):
            raise ValueError("hop_strategy must be consensus, relevance, semantic")
        inert = strategy is not None and not bound.arguments.get("multi_hop", False)
        result = fn(*bound.args, **bound.kwargs)
        if inert:
            marker = {"hop_strategy": "multi_hop is false"}
            result = (
                {**result, "inert_parameters": marker}
                if isinstance(result, dict)
                else str(result) + "\n" + json.dumps({"inert_parameters": marker})
            )
        if not window:
            return result
        details = {"window": window, "coverage": search_window_coverage(**window)}
        if isinstance(result, dict):
            return {**result, **details}
        return str(result) + "\n" + json.dumps(details)

    return wrapped
