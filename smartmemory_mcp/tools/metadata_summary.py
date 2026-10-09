"""Compact, allowlist-based metadata summary for model-facing tool output.

Raw item metadata is mostly bookkeeping (tenant and security IDs, activation,
retrieval stats, full conflict fact bodies). Model-facing output shows only what
an agent can act on: dates, source/provenance, tags and conflicts.

An allowlisted KEY is not an allowlisted VALUE. Every kept value is therefore
type-checked (scalars only), single-lined, bounded and scrubbed (URL credentials
and query strings, ``tenant_id=...`` style pairs, identity path segments).
Compound values are dropped, never repr-printed, and every drop logs a WARNING
naming the key. Anything not named here stays out, so new metadata keys are
hidden by default.

One collector (``collect_item_metadata``) feeds both the prose summary used by
``memory_get`` and the structured ``compact_item`` used by citation, working
context, read-around and effects payloads.
"""

import logging
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_MAX_FIELD_CHARS = 160
_MAX_ID_CHARS = 80
_MAX_TYPE_CHARS = 60
_MAX_TAG_CHARS = 60
_MAX_CONFLICT_LINES = 20
_MAX_TAGS = 20
_MAX_RESOLVED_DATES = 10

_SOURCE_KEYS = ("source", "origin", "provenance")
_CONFLICT_ID_KEYS = ("existing_item_id", "conflicting_item_id", "item_id", "id")

_SENSITIVE_NAMES = (
    "tenant|workspace|team|owner|user|security|session|run|account|org|organization"
)
_SECRET_NAMES = (
    "api[_-]?key|apikey|token|secret|password|passwd|authorization|auth|credential|"
    "access[_-]?key|signature|sig"
)
# ``tenant_id=abc``, ``owner_key: abc``, ``api_key=abc``, ``Bearer abc``.
_PAIR_RE = re.compile(
    rf"(?i)\b((?:{_SENSITIVE_NAMES})[_-]?(?:id|key|scope|uuid)|{_SECRET_NAMES})"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
_IDENTITY_SEGMENT_RE = re.compile(rf"(?i)^(?:{_SENSITIVE_NAMES})s?$")
_REDACTED = "[redacted]"
IDENTITY_KEYS = frozenset({"tenant_id", "workspace_id", "team_id", "user_id", "run_id"})
_SCALARS = (str, int, float)


def _warn(item: Any, key: str, why: str) -> None:
    logger.warning(
        "Metadata summary dropped %r for %s: %s; use include_metadata=True or "
        "memory_explain for the raw value.",
        key,
        item.get("item_id") if isinstance(item, dict) else None,
        why,
    )


def _scrub_url(match: "re.Match[str]") -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;)":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        segments = parts.path.split("/")
        for index in range(1, len(segments) - 1):
            if _IDENTITY_SEGMENT_RE.match(segments[index]) and segments[index + 1]:
                segments[index + 1] = _REDACTED
        return urlunsplit((parts.scheme, host, "/".join(segments), "", "")) + trailing
    except ValueError:
        return _REDACTED + trailing


def _scrub(text: str) -> str:
    text = _URL_RE.sub(_scrub_url, text)
    text = _PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)


def _text(value: Any, limit: int = _MAX_FIELD_CHARS) -> Optional[str]:
    """Bounded, single-line, scrubbed text for a scalar; ``None`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, _SCALARS):
        return None
    text = _scrub(" ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _cap(text: str, limit: int = 240) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else None


def _conflict_rows(item: Dict[str, Any], metadata: Dict[str, Any]) -> List[Any]:
    """Raw conflict entries from ``challenge_result.conflicts`` or ``conflicts``."""
    raw = None
    challenge = metadata.get("challenge_result")
    if isinstance(challenge, dict):
        raw = challenge.get("conflicts")
    elif challenge is not None:
        _warn(
            item, "challenge_result", f"expected a dict, got {type(challenge).__name__}"
        )
    if raw is None:
        raw = metadata.get("conflicts")
    if raw is None:
        return []
    if isinstance(raw, dict):
        if isinstance(raw.get("conflicts"), list):
            return list(raw["conflicts"])
        if any(k in raw for k in (*_CONFLICT_ID_KEYS, "conflict_type")):
            return [raw]  # a single conflict dict
        return [
            {"existing_item_id": key, **value} if isinstance(value, dict) else value
            for key, value in raw.items()
        ]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    _warn(item, "conflicts", f"expected a list or dict, got {type(raw).__name__}")
    return []


def _conflicts(item: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    rows = _conflict_rows(item, metadata)
    out: List[Dict[str, str]] = []
    for row in rows[:_MAX_CONFLICT_LINES]:
        if not isinstance(row, dict):
            _warn(item, "conflicts", f"entry is {type(row).__name__}, not a dict")
            continue
        other = next(
            (t for k in _CONFLICT_ID_KEYS if (t := _text(row.get(k), _MAX_ID_CHARS))),
            None,
        )
        if other is None and any(row.get(k) for k in _CONFLICT_ID_KEYS):
            _warn(item, "conflicts.existing_item_id", "not a scalar id")
        kind = _text(row.get("conflict_type"), _MAX_TYPE_CHARS)
        if kind is None and row.get("conflict_type"):
            _warn(item, "conflicts.conflict_type", "not a scalar")
        if other is None and kind is None and not row.get("explanation"):
            _warn(item, "conflicts", "entry has no usable id, type or explanation")
            continue
        entry = {"existing_item_id": other or "?", "conflict_type": kind or "unknown"}
        explanation = _text(row.get("explanation"))
        if explanation is None and row.get("explanation"):
            _warn(item, "conflicts.explanation", "not a scalar")
        if explanation:
            entry["explanation"] = explanation
        out.append(entry)
    if len(rows) > _MAX_CONFLICT_LINES:
        logger.warning(
            "Metadata summary capped conflicts for %s at %d of %d.",
            _get(item, "item_id"),
            _MAX_CONFLICT_LINES,
            len(rows),
        )
    return {"conflicts": out, "more": max(0, len(rows) - _MAX_CONFLICT_LINES)}


def _scalar_field(item: Dict[str, Any], key: str, value: Any) -> Optional[str]:
    text = _text(value)
    if text is None and value:
        _warn(item, key, f"expected a scalar, got {type(value).__name__}")
    return text or None


def _resolved_dates(item: Dict[str, Any], value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        text = _scalar_field(item, "resolved_dates", value)
        return [text] if text else []
    dates: List[str] = []
    for entry in value[:_MAX_RESOLVED_DATES]:
        candidate = (
            (entry.get("date") or entry.get("text"))
            if isinstance(entry, dict)
            else entry
        )
        text = _text(candidate)
        if text:
            dates.append(text)
        elif candidate:
            _warn(item, "resolved_dates", f"entry is {type(candidate).__name__}")
    if len(value) > _MAX_RESOLVED_DATES:
        logger.warning(
            "Metadata summary capped resolved_dates for %s at %d of %d.",
            _get(item, "item_id"),
            _MAX_RESOLVED_DATES,
            len(value),
        )
    return dates


def _tags(item: Dict[str, Any], value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        _warn(item, "tags", f"expected a list, got {type(value).__name__}")
        return []
    tags: List[str] = []
    dropped = 0
    for tag in value[:_MAX_TAGS]:
        text = _text(tag, _MAX_TAG_CHARS)
        if text:
            tags.append(text)
        else:
            dropped += 1
    if dropped:
        _warn(item, "tags", f"{dropped} non-scalar or empty tag(s)")
    if len(value) > _MAX_TAGS:
        logger.warning(
            "Metadata summary capped tags for %s at %d of %d.",
            _get(item, "item_id"),
            _MAX_TAGS,
            len(value),
        )
    return tags


def collect_item_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitised allowlisted metadata for ``item``; never raises.

    Keys (all optional): ``created_at``, ``valid_start_time``, ``valid_end_time``,
    ``resolved_dates`` (list), ``source``, ``origin``, ``provenance``, ``tags``
    (list), ``conflicts`` (list of ``{existing_item_id, conflict_type,
    explanation?}``), ``conflicts_more`` (int, when capped). Values are bounded single-line scrubbed text.
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
    if not isinstance(item, dict):
        return {}
    out: Dict[str, Any] = {}

    def section(name: str, build) -> None:
        try:
            build()
        except Exception as exc:
            logger.warning(
                "Metadata summary section %s failed for %s: %s; that section is "
                "not shown (use include_metadata=True).",
                name,
                _get(item, "item_id"),
                exc,
            )

    def dates() -> None:
        for key, value in (
            ("created_at", item.get("created_at") or metadata.get("created_at")),
            ("valid_start_time", item.get("valid_start_time")),
            ("valid_end_time", item.get("valid_end_time")),
        ):
            text = _scalar_field(item, key, value)
            if text:
                out[key] = text
        resolved = metadata.get("resolved_dates")
        resolved = resolved if resolved else metadata.get("resolved_date")
        found = _resolved_dates(item, resolved) if resolved else []
        if found:
            out["resolved_dates"] = found

    def sources() -> None:
        for key in _SOURCE_KEYS:
            text = _scalar_field(item, key, metadata.get(key) or item.get(key))
            if text:
                out[key] = text
        if metadata.get("tags"):
            tags = _tags(item, metadata["tags"])
            if tags:
                out["tags"] = tags

    def conflicts() -> None:
        found = _conflicts(item, metadata)
        if found["conflicts"]:
            out["conflicts"] = found["conflicts"]
        if found["more"]:
            out["conflicts_more"] = found["more"]

    section("dates", dates)
    section("sources", sources)
    section("conflicts", conflicts)
    return out


def summarize_item_metadata(item: Dict[str, Any]) -> List[str]:
    """Return compact prose summary lines for ``item``; never raises."""
    data = collect_item_metadata(item)
    lines: List[str] = []
    if "created_at" in data:
        lines.append(f"Created: {data['created_at']}")
    if "valid_start_time" in data or "valid_end_time" in data:
        lines.append(
            f"Valid: {data.get('valid_start_time', '?')} to "
            f"{data.get('valid_end_time', 'open')}"
        )
    if "resolved_dates" in data:
        lines.append("Resolved dates: " + _cap(", ".join(data["resolved_dates"])))
    for key in _SOURCE_KEYS:
        if key in data:
            lines.append(f"{key.capitalize()}: {data[key]}")
    if "tags" in data:
        lines.append("Tags: " + _cap(", ".join(data["tags"])))
    for conflict in data.get("conflicts", []):
        line = f"conflicts with {conflict['existing_item_id']}: {conflict['conflict_type']}"
        if "explanation" in conflict:
            line += f": {conflict['explanation']}"
        lines.append(line)
    if data.get("conflicts_more"):
        lines.append(f"(+{data['conflicts_more']} more conflicts)")
    return lines


def compact_item(item: Any, keep_metadata_keys: Iterable[str] = ()) -> Any:
    """Copy of a structured item with ``metadata`` replaced by the allowlisted summary.

    ``keep_metadata_keys`` names intentional payload keys (for example
    ``effects_bundle``) carried over verbatim. Non-dict items pass through.
    Top-level fields (id, type, content, scores...) are kept except server-only
    identity keys; an unusable ``metadata`` value is replaced, never repr-ed.
    """
    if not isinstance(item, dict):
        return item
    compact = {k: v for k, v in item.items() if k not in IDENTITY_KEYS}
    if "metadata" not in item:
        return compact
    metadata = collect_item_metadata(item)
    raw = item.get("metadata")
    if isinstance(raw, dict):
        for key in keep_metadata_keys:
            if key in raw:
                metadata[key] = raw[key]
    compact["metadata"] = metadata
    return compact
