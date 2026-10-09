"""Compact, allowlist-based metadata summary for model-facing tool output.

Raw item metadata is mostly bookkeeping (tenant and security IDs, activation,
retrieval stats, full conflict fact bodies). The summary shows only what an agent
can act on: dates, source/provenance, tags and one line per conflict. Anything
not named here stays out, so new metadata keys are hidden by default.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_FIELD_CHARS = 160
_MAX_CONFLICT_LINES = 20
_MAX_TAGS = 20

_SOURCE_KEYS = ("source", "origin", "provenance")
_CONFLICT_ID_KEYS = ("existing_item_id", "conflicting_item_id", "item_id", "id")


def _short(value: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else None


def _conflict_rows(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Conflict dicts from ``challenge_result.conflicts`` or a top-level ``conflicts``."""
    raw = None
    challenge = metadata.get("challenge_result")
    if isinstance(challenge, dict):
        raw = challenge.get("conflicts")
    if raw is None:
        raw = metadata.get("conflicts")
    if isinstance(raw, dict):
        # Either {"conflicts": [...]} or {<existing id>: {...}}.
        if isinstance(raw.get("conflicts"), list):
            raw = raw["conflicts"]
        else:
            raw = [
                {"existing_item_id": key, **value}
                for key, value in raw.items()
                if isinstance(value, dict)
            ]
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _conflict_lines(metadata: Dict[str, Any]) -> List[str]:
    rows = _conflict_rows(metadata)
    lines = []
    for row in rows[:_MAX_CONFLICT_LINES]:
        other = next((row[k] for k in _CONFLICT_ID_KEYS if row.get(k)), "?")
        kind = row.get("conflict_type") or "unknown"
        explanation = row.get("explanation")
        line = f"conflicts with {other}: {kind}"
        if explanation:
            line += f": {_short(explanation)}"
        lines.append(line)
    if len(rows) > _MAX_CONFLICT_LINES:
        lines.append(f"(+{len(rows) - _MAX_CONFLICT_LINES} more conflicts)")
    return lines


def _date_lines(item: Dict[str, Any], metadata: Dict[str, Any]) -> List[str]:
    lines = []
    created = item.get("created_at") or metadata.get("created_at")
    if created:
        lines.append(f"Created: {_short(created)}")
    start, end = item.get("valid_start_time"), item.get("valid_end_time")
    if start or end:
        lines.append(f"Valid: {_short(start or '?')} to {_short(end or 'open')}")
    resolved = metadata.get("resolved_dates")
    if isinstance(resolved, (list, tuple)) and resolved:
        dates = [
            d.get("date") or d.get("text") if isinstance(d, dict) else d
            for d in resolved
        ]
        lines.append("Resolved dates: " + _short(", ".join(str(d) for d in dates if d)))
    elif metadata.get("resolved_date"):
        lines.append(f"Resolved date: {_short(metadata['resolved_date'])}")
    return lines


def _source_lines(item: Dict[str, Any], metadata: Dict[str, Any]) -> List[str]:
    lines = []
    for key in _SOURCE_KEYS:
        value = metadata.get(key) or item.get(key)
        if value and isinstance(value, (str, int, float)):
            lines.append(f"{key.capitalize()}: {_short(value)}")
    tags = metadata.get("tags")
    if isinstance(tags, (list, tuple)) and tags:
        shown = ", ".join(str(t) for t in tags[:_MAX_TAGS])
        lines.append(f"Tags: {_short(shown)}")
    return lines


def summarize_item_metadata(item: Dict[str, Any]) -> List[str]:
    """Return compact summary lines for ``item``; never raises.

    Malformed metadata yields no lines for the affected section and a WARNING.
    """
    metadata = _get(item, "metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        logger.warning(
            "Metadata summary skipped for %s: metadata is %s, not a dict; "
            "conflicts, dates and source are not shown (use include_metadata=True).",
            _get(item, "item_id"),
            type(metadata).__name__,
        )
        metadata = {}
    lines: List[str] = []
    for section in (_date_lines, _source_lines, _conflict_lines):
        try:
            if section is _conflict_lines:
                lines.extend(section(metadata))
            else:
                lines.extend(section(item, metadata))
        except Exception as exc:
            logger.warning(
                "Metadata summary section %s failed for %s: %s; that section is "
                "not shown (use include_metadata=True).",
                section.__name__,
                _get(item, "item_id"),
                exc,
            )
    return lines
