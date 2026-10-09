"""Allowlisted, type-validated item output for model-facing tools (MCP-MEMGET-META-1).

Raw item metadata is mostly bookkeeping (tenant and security IDs, activation,
retrieval stats, full conflict fact bodies). Model-facing output shows only what
an agent can act on: dates, source/provenance, tags and conflicts.

The output is BUILT, not filtered. ``compact_item`` emits only the top-level
fields in ``_TOP_LEVEL`` and only the metadata keys produced by
``collect_item_metadata``. Every value is validated by type, never scrubbed:

- dates must parse as ISO dates/datetimes and are re-emitted from the parse;
- URLs must be http/https/ftp/git+ssh with a conventional authority and are
  rebuilt as ``scheme://host[:port]/path`` (no userinfo, query or fragment);
- labels, tags, ids and conflict types must match a bounded charset;
- free text (conflict explanations) is bounded, and any string carrying a
  credential or identity marker (checked after percent, HTML, JSON and
  unicode-escape decoding) is dropped whole, never partially redacted.

Anything else is dropped. Every drop and every truncation logs a WARNING that
names the key and the reason, never the value. Keys left out by the allowlist
are named in one WARNING per item. ``None`` and blank strings are absence, not
data, and are skipped quietly. Compaction is idempotent, so a second pass keeps
the same output (including ``conflicts_more``).

One collector feeds both the prose summary (``memory_get``) and the structured
items (search/recall citations, working context, read-around, hosted effects).
"""

import codecs
import html
import ipaddress
import json
import logging
import math
import re
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote_plus, urlsplit

logger = logging.getLogger(__name__)

_MAX_FIELD_CHARS = 160
_MAX_LABEL_CHARS = 80
_MAX_URL_CHARS = 200
_MAX_URL_INPUT_CHARS = 2048
_MAX_FREE_TEXT_INPUT_CHARS = 20_000
_MAX_ID_CHARS = 80
_MAX_ITEM_ID_CHARS = 128
_MAX_TYPE_CHARS = 60
_MAX_TAG_CHARS = 60
_MAX_DATE_INPUT_CHARS = 40
_MAX_CONFLICT_LINES = 20
_MAX_TAGS = 20
_MAX_RESOLVED_DATES = 10
_MAX_SCAN = 200
_MAX_DECODE_ROUNDS = 3
_MAX_JSON_STRINGS = 200
_MAX_LOGGED_KEYS = 40

_SOURCE_KEYS = ("source", "origin", "provenance")
_DATE_KEYS = ("created_at", "valid_start_time", "valid_end_time")
_CONFLICT_ID_KEYS = ("existing_item_id", "conflicting_item_id", "item_id", "id")
_CONFLICT_KEYS = frozenset((*_CONFLICT_ID_KEYS, "conflict_type", "explanation"))
# Metadata keys the collector reads. Everything else is omitted and named.
_METADATA_KEYS = frozenset(
    (
        *_DATE_KEYS,
        *_SOURCE_KEYS,
        "resolved_dates",
        "resolved_date",
        "tags",
        "conflicts",
        "conflicts_more",
        "challenge_result",
    )
)
# Top-level copies folded into ``metadata`` by the collector, never emitted raw.
_FOLDED_TOP_LEVEL = frozenset((*_DATE_KEYS, *_SOURCE_KEYS, "transaction_time"))
_SCORE_BREAKDOWN_KEYS = frozenset(
    (
        "activation",
        "relevance",
        "recency",
        "centrality",
        "anchor_forced",
        "session_pin_boost",
        "freshness_boost",
    )
)
_AS_OF_RESOLUTIONS = frozenset(("resolved", "unresolved", "no_chain"))
# Kept for importers; identity keys are now dropped by the allowlist itself.
IDENTITY_KEYS = frozenset({"tenant_id", "workspace_id", "team_id", "user_id", "run_id"})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_ :-]*")
_TAG_RE = re.compile(r"[^\W_][\w .:#+-]*")
_LABEL_RE = re.compile(r"[^\W_][\w .:+/-]*")
_KEY_NAME_RE = re.compile(r"[\w.\[\]-]{1,64}")
_URL_SCHEME_RE = re.compile(r"(?i)(https?|ftp|git\+ssh)://")
_DNS_RE = re.compile(
    r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~!$'()*+,:%-]*)*")
_ENCODED_SEPARATOR_RE = re.compile(r"(?i)%(?:2f|5c|3f|23|40|3a|3b|3d|26|25|00)")
# Opaque URI schemes are never plain labels (``mailto:x``, ``data:...``).
_OPAQUE_SCHEMES = frozenset(
    (
        "mailto",
        "tel",
        "sms",
        "data",
        "javascript",
        "vbscript",
        "file",
        "urn",
        "ldap",
        "ldaps",
        "sip",
        "sips",
        "xmpp",
        "magnet",
        "news",
        "nntp",
        "git",
        "ssh",
        "http",
        "https",
        "ftp",
        "ws",
        "wss",
        "s3",
        "gs",
        "blob",
    )
)

_IDENTITY_NOUNS = (
    r"tenant|workspace|team|owner|user|security|session|run|account|org|"
    r"organi[sz]ation|customer|member|principal|subject"
)
_MARKER_RES = (
    # tenant_id, owner-key, "workspace id", sessionUuid, security_scope ...
    re.compile(rf"(?i)(?:{_IDENTITY_NOUNS})[\W_]*(?:id|key|scope|uuid|token)(?![a-z])"),
    # credentials by name
    re.compile(
        r"(?i)api[\W_]*key|access[\W_]*key|private[\W_]*key|secret|passw(?:or)?d|"
        r"(?<![a-z])pwd(?![a-z])|token|bearer|authori[sz]ation|credential|signature|"
        r"cookie|x-amz-"
    ),
    # userinfo and e-mail addresses: ``u:p@host``, ``alice@example.com``
    re.compile(r"[^\s@/]@[^\s@]"),
    # identity path segments: ``tenants/abc``, ``/Users/alice`` (``/org/repo``
    # stays allowed: forge organisations are public names, not tenant ids)
    re.compile(
        r"(?i)(?:^|[/\\])(?:tenant|workspace|team|owner|user|account|"
        r"customer|member)s?[/\\]+[^/\\\s]"
    ),
    # ``tenant:abc`` / ``owner=abc`` (``user:zettel`` origin tiers stay allowed)
    re.compile(
        r"(?i)(?<![a-z])(?:tenant|workspace|team|owner|account|org|"
        r"organi[sz]ation|customer)s?\s*[:=]"
    ),
    re.compile(r"(?i)(?<![a-z])users?\s*="),
    # query-string parameters
    re.compile(r"[?&][\w.\-\[\]]+="),
    # well-known token shapes
    re.compile(
        r"\beyJ[\w-]{8,}|\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
        r"\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-|\bb44[uk]_"
    ),
)

_Result = Tuple[Any, str]  # (value or None, note); a note on a kept value = altered


# ---------------------------------------------------------------------------
# Logging: name the key and the reason, never the value.
# ---------------------------------------------------------------------------


def _key_name(key: Any) -> str:
    text = str(key) if isinstance(key, (str, int)) else ""
    return text if _KEY_NAME_RE.fullmatch(text) else f"<{type(key).__name__} key>"


class _Log:
    """Per-item drop log. ``drop`` warns at once; ``omit`` is batched per item."""

    def __init__(self, item: Any):
        raw = item.get("item_id") if isinstance(item, dict) else None
        valid, _ = _ident(raw, _MAX_ITEM_ID_CHARS) if raw is not None else (None, "")
        self.item_id = valid if valid else ("<invalid id>" if raw is not None else None)
        self.omitted: List[str] = []

    def drop(self, key: str, why: str) -> None:
        logger.warning(
            "Metadata summary dropped %s for item %s: %s; use include_metadata=True "
            "or memory_explain for the raw value.",
            key,
            self.item_id,
            why,
        )

    def altered(self, key: str, why: str) -> None:
        logger.warning(
            "Metadata summary shortened %s for item %s: %s; use include_metadata=True "
            "or memory_explain for the raw value.",
            key,
            self.item_id,
            why,
        )

    def omit(self, key: str) -> None:
        if key not in self.omitted:
            self.omitted.append(key)

    def flush(self) -> None:
        if not self.omitted:
            return
        shown = self.omitted[:_MAX_LOGGED_KEYS]
        extra = len(self.omitted) - len(shown)
        logger.warning(
            "Metadata summary omitted non-allowlisted keys for item %s: %s%s; use "
            "include_metadata=True or memory_explain for the raw values.",
            self.item_id,
            ", ".join(shown),
            f" (+{extra} more)" if extra else "",
        )
        self.omitted = []


def _absent(value: Any) -> bool:
    """``None`` and blank strings carry nothing; they are absence, not a drop."""
    return value is None or (isinstance(value, str) and not value.strip())


def _type_name(value: Any) -> str:
    return type(value).__name__


# ---------------------------------------------------------------------------
# Marker detection over decoded views.
# ---------------------------------------------------------------------------


def _json_strings(obj: Any, out: List[str], depth: int = 0) -> None:
    if len(out) >= _MAX_JSON_STRINGS or depth > 20:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.append(str(key))
            _json_strings(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _json_strings(value, out, depth + 1)
    elif obj is not None:
        out.append(str(obj))


def _decoded_views(text: str) -> List[str]:
    views = [text]
    current = text
    for _ in range(_MAX_DECODE_ROUNDS):
        decoded = unquote_plus(current)
        if decoded == current:
            break
        views.append(decoded)
        current = decoded
    for view in list(views):
        unescaped = html.unescape(view)
        if unescaped != view:
            views.append(unescaped)
    for view in list(views):
        if "\\" in view:
            try:
                views.append(codecs.decode(view, "unicode_escape"))
            except (UnicodeDecodeError, ValueError):
                pass
    for view in list(views):
        stripped = view.strip()
        if stripped[:1] in ("{", "[", '"'):
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                continue
            strings: List[str] = []
            _json_strings(parsed, strings)
            views.append(" ".join(strings))
    return views


def _has_marker(text: str) -> bool:
    return any(
        pattern.search(view) for view in _decoded_views(text) for pattern in _MARKER_RES
    )


# ---------------------------------------------------------------------------
# Value validators: (value, "") kept, (value, note) kept but altered,
# (None, reason) dropped.
# ---------------------------------------------------------------------------


def _date(value: Any) -> _Result:
    if isinstance(value, datetime):
        return value.isoformat(), ""
    if isinstance(value, date):
        return value.isoformat(), ""
    if not isinstance(value, str):
        return None, f"expected an ISO date string, got {_type_name(value)}"
    text = value.strip()
    if len(text) > _MAX_DATE_INPUT_CHARS:
        return None, "not an ISO date (too long)"
    for parse in (date.fromisoformat, datetime.fromisoformat):
        try:
            return parse(text).isoformat(), ""
        except ValueError:
            continue
    return None, "not an ISO date or datetime"


def _ident(value: Any, limit: int = _MAX_ID_CHARS) -> _Result:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None, f"expected an id, got {_type_name(value)}"
    text = str(value).strip()
    if len(text) > limit:
        return None, f"id longer than {limit} chars"
    if not _ID_RE.fullmatch(text):
        return None, "not id-shaped"
    if _has_marker(text):
        return None, "contains a credential or identity marker"
    return text, ""


def _token(
    pattern: "re.Pattern[str]", limit: int, what: str
) -> Callable[[Any], _Result]:
    def validate(value: Any) -> _Result:
        if not isinstance(value, str):
            return None, f"expected a {what} string, got {_type_name(value)}"
        text = " ".join(value.split())
        if len(text) > limit:
            return None, f"{what} longer than {limit} chars"
        if not pattern.fullmatch(text):
            return None, f"{what} has characters outside the allowed set"
        if _has_marker(text):
            return None, "contains a credential or identity marker"
        return text, ""

    return validate


_tag = _token(_TAG_RE, _MAX_TAG_CHARS, "tag")
_conflict_type = _token(_TYPE_RE, _MAX_TYPE_CHARS, "conflict type")
_memory_type = _token(_TYPE_RE, _MAX_TYPE_CHARS, "memory type")


def _url(text: str) -> _Result:
    if len(text) > _MAX_URL_INPUT_CHARS:
        return None, "URL too long"
    scheme = _URL_SCHEME_RE.match(text)
    if scheme is None:
        return None, "not an http, https, ftp or git+ssh URL"
    if any(ch.isspace() or not ch.isprintable() or ch == "\\" for ch in text):
        return None, "URL has whitespace, control or backslash characters"
    if text[scheme.end() :].startswith("/"):
        return None, "URL has an empty authority (extra slashes)"
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None, "URL authority does not parse"
    if not host or "%" in host:
        return None, "URL has no conventional host"
    if ":" in host:
        try:
            authority = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError:
            return None, "URL host is not a valid IPv6 address"
    else:
        try:
            authority = str(ipaddress.IPv4Address(host))
        except ValueError:
            if not _DNS_RE.fullmatch(host):
                return None, "URL host is not a DNS name or IP address"
            authority = host
    if port is not None:
        authority = f"{authority}:{port}"
    path = parts.path
    if not _PATH_RE.fullmatch(path):
        return None, "URL path has characters outside the allowed set"
    if _ENCODED_SEPARATOR_RE.search(path):
        return None, "URL path has percent-encoded separators"
    if _has_marker(host) or _has_marker(path):
        return None, "URL contains a credential or identity marker"
    rebuilt = f"{scheme.group(1).lower()}://{authority}{path}"
    if len(rebuilt) > _MAX_URL_CHARS:
        return None, f"URL longer than {_MAX_URL_CHARS} chars"
    stripped = "@" in parts.netloc or parts.query or parts.fragment
    stripped = stripped or text.endswith(("?", "#"))
    return rebuilt, ("removed userinfo, query or fragment" if stripped else "")


def _source(value: Any) -> _Result:
    """A source/origin/provenance value: an allowed URL or a plain label."""
    if not isinstance(value, str):
        return None, f"expected a label or URL string, got {_type_name(value)}"
    text = value.strip()
    if "://" in text or text.startswith(("//", "\\\\")):
        return _url(text)
    if len(text) > _MAX_LABEL_CHARS:
        return None, f"label longer than {_MAX_LABEL_CHARS} chars"
    if not _LABEL_RE.fullmatch(text) or "//" in text:
        return None, "not a plain label or an allowed URL"
    if ":" in text and text.split(":", 1)[0].lower() in _OPAQUE_SCHEMES:
        return None, "opaque URI scheme"
    if _has_marker(text):
        return None, "contains a credential or identity marker"
    return text, ""


def _free_text(value: Any, limit: int = _MAX_FIELD_CHARS) -> _Result:
    if not isinstance(value, str):
        return None, f"expected text, got {_type_name(value)}"
    text = "".join(ch for ch in " ".join(value.split()) if ch.isprintable())
    if len(text) > _MAX_FREE_TEXT_INPUT_CHARS:
        return None, f"text longer than {_MAX_FREE_TEXT_INPUT_CHARS} chars"
    if _has_marker(text):
        return None, "contains a credential or identity marker"
    if len(text) > limit:
        return text[: limit - 3] + "...", f"truncated from {len(text)} to {limit} chars"
    return text, ""


def _number(value: Any) -> _Result:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"expected a number, got {_type_name(value)}"
    if not math.isfinite(value):
        return None, "not a finite number"
    return value, ""


def _boolean(value: Any) -> _Result:
    if isinstance(value, bool):
        return value, ""
    return None, f"expected a boolean, got {_type_name(value)}"


def _content(value: Any) -> _Result:
    if isinstance(value, str):
        return value, ""
    return None, f"expected text content, got {_type_name(value)}"


def _as_of(value: Any) -> _Result:
    if isinstance(value, str) and value in _AS_OF_RESOLUTIONS:
        return value, ""
    return None, "not a known as_of_resolution value"


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


def _pick(
    log: _Log,
    candidates: Iterable[Tuple[str, Any]],
    validate: Callable[[Any], _Result],
) -> Any:
    """First valid candidate wins; every other non-absent candidate that is not
    the same shown value is dropped with a WARNING."""
    chosen = None
    for name, raw in candidates:
        if _absent(raw):
            continue
        value, note = validate(raw)
        if value is None:
            log.drop(name, note)
        elif chosen is None:
            chosen = value
            if note:
                log.altered(name, note)
        elif value != chosen:
            log.drop(name, "a different value is already shown")
    return chosen


def _as_list(log: _Log, name: str, raw: Any) -> List[Tuple[str, Any]]:
    """Named entries of a list-typed key. Empty lists are absence."""
    if _absent(raw):
        return []
    if isinstance(raw, (list, tuple)):
        return [(f"{name}[{index}]", entry) for index, entry in enumerate(raw)]
    log.drop(name, f"expected a list, got {_type_name(raw)}")
    return []


def _resolved_dates(log: _Log, metadata: Dict[str, Any]) -> List[str]:
    entries: List[Tuple[str, Any]] = []
    for key in ("resolved_dates", "resolved_date"):
        raw = metadata.get(key)
        if isinstance(raw, (list, tuple)):
            entries.extend(_as_list(log, f"metadata.{key}", raw))
        elif not _absent(raw):
            entries.append((f"metadata.{key}", raw))
    dates: List[str] = []
    for name, entry in entries[:_MAX_SCAN]:
        if isinstance(entry, dict):
            candidates = [(f"{name}.{k}", entry.get(k)) for k in ("date", "text")]
            if all(_absent(raw) for _, raw in candidates):
                log.drop(name, "entry has no date")
                continue
            value = _pick(log, candidates, _date)
        elif _absent(entry):
            continue
        else:
            value = _pick(log, [(name, entry)], _date)
        if value is None or value in dates:
            continue
        if len(dates) >= _MAX_RESOLVED_DATES:
            log.drop(name, f"resolved dates capped at {_MAX_RESOLVED_DATES}")
            continue
        dates.append(value)
    if len(entries) > _MAX_SCAN:
        log.drop("metadata.resolved_dates", f"capped scan at {_MAX_SCAN} entries")
    return dates


def _tags(log: _Log, metadata: Dict[str, Any]) -> List[str]:
    entries = _as_list(log, "metadata.tags", metadata.get("tags"))
    tags: List[str] = []
    for name, entry in entries[:_MAX_TAGS]:
        value = _pick(log, [(name, entry)], _tag)
        if value is not None and value not in tags:
            tags.append(value)
    if len(entries) > _MAX_TAGS:
        log.drop("metadata.tags", f"capped at {_MAX_TAGS} of {len(entries)} tags")
    return tags


def _conflict_rows(log: _Log, metadata: Dict[str, Any]) -> List[Tuple[str, Any]]:
    rows: List[Tuple[str, Any]] = []
    challenge = metadata.get("challenge_result")
    if isinstance(challenge, dict):
        for key in challenge:
            if key != "conflicts":
                log.omit(f"metadata.challenge_result.{_key_name(key)}")
        rows.extend(
            _shape_rows(
                log, "metadata.challenge_result.conflicts", challenge.get("conflicts")
            )
        )
    elif not _absent(challenge):
        log.drop(
            "metadata.challenge_result", f"expected a dict, got {_type_name(challenge)}"
        )
    rows.extend(_shape_rows(log, "metadata.conflicts", metadata.get("conflicts")))
    return rows


def _shape_rows(log: _Log, name: str, raw: Any) -> List[Tuple[str, Any]]:
    if isinstance(raw, dict):
        if isinstance(raw.get("conflicts"), list):
            return _as_list(log, f"{name}.conflicts", raw["conflicts"])
        if any(k in raw for k in (*_CONFLICT_ID_KEYS, "conflict_type", "explanation")):
            return [(name, raw)]  # a single conflict dict
        return [
            (
                f"{name}[{_key_name(key)}]",
                {"existing_item_id": key, **value}
                if isinstance(value, dict)
                else value,
            )
            for key, value in raw.items()
        ]
    return _as_list(log, name, raw)


def _conflicts(log: _Log, metadata: Dict[str, Any]) -> Tuple[List[Dict[str, str]], int]:
    rows = _conflict_rows(log, metadata)
    out: List[Dict[str, str]] = []
    for name, row in rows[:_MAX_CONFLICT_LINES]:
        if not isinstance(row, dict):
            if not _absent(row):
                log.drop(name, f"expected a conflict dict, got {_type_name(row)}")
            continue
        for key in row:
            if key not in _CONFLICT_KEYS:
                log.omit(f"conflicts[].{_key_name(key)}")
        other = _pick(
            log, [(f"{name}.{k}", row.get(k)) for k in _CONFLICT_ID_KEYS], _ident
        )
        kind = _pick(
            log, [(f"{name}.conflict_type", row.get("conflict_type"))], _conflict_type
        )
        explanation = _pick(
            log, [(f"{name}.explanation", row.get("explanation"))], _free_text
        )
        if other is None and kind is None and explanation is None:
            log.drop(name, "entry has no usable id, type or explanation")
            continue
        entry: Dict[str, str] = {}
        if other is not None:
            entry["existing_item_id"] = other
        if kind is not None:
            entry["conflict_type"] = kind
        if explanation is not None:
            entry["explanation"] = explanation
        if entry not in out:
            out.append(entry)
    more = max(0, len(rows) - _MAX_CONFLICT_LINES)
    if more:
        log.drop("conflicts", f"capped at {_MAX_CONFLICT_LINES} of {len(rows)} entries")
    carried = metadata.get("conflicts_more")
    if not _absent(carried):
        if isinstance(carried, int) and not isinstance(carried, bool) and carried >= 0:
            more += carried
        else:
            log.drop("metadata.conflicts_more", "expected a non-negative integer")
    return out, more


def _collect(
    item: Dict[str, Any], log: _Log, keep: Iterable[str] = ()
) -> Dict[str, Any]:
    keep = frozenset(keep)
    metadata = item.get("metadata")
    if _absent(metadata):
        metadata = {}
    elif not isinstance(metadata, dict):
        log.drop(
            "metadata",
            f"metadata is {_type_name(metadata)}, not a dict; conflicts, dates and "
            "source are not shown",
        )
        metadata = {}
    for key in metadata:
        if key not in _METADATA_KEYS and key not in keep:
            log.omit(f"metadata.{_key_name(key)}")
    out: Dict[str, Any] = {}

    def section(name: str, build: Callable[[], None]) -> None:
        try:
            build()
        except Exception as exc:  # never raise out of the output boundary
            log.drop(name, f"section failed ({type(exc).__name__}); not shown")

    def dates() -> None:
        for key in _DATE_KEYS:
            candidates = [(key, item.get(key)), (f"metadata.{key}", metadata.get(key))]
            if key == "created_at":
                candidates.insert(1, ("transaction_time", item.get("transaction_time")))
            value = _pick(log, candidates, _date)
            if value is not None:
                out[key] = value
        resolved = _resolved_dates(log, metadata)
        if resolved:
            out["resolved_dates"] = resolved

    def sources() -> None:
        for key in _SOURCE_KEYS:
            value = _pick(
                log,
                [(f"metadata.{key}", metadata.get(key)), (key, item.get(key))],
                _source,
            )
            if value is not None:
                out[key] = value
        tags = _tags(log, metadata)
        if tags:
            out["tags"] = tags

    def conflicts() -> None:
        found, more = _conflicts(log, metadata)
        if found:
            out["conflicts"] = found
        if more:
            out["conflicts_more"] = more

    section("dates", dates)
    section("sources", sources)
    section("conflicts", conflicts)
    return out


def collect_item_metadata(item: Any, keep: Iterable[str] = ()) -> Dict[str, Any]:
    """Validated allowlisted metadata for ``item``; never raises.

    Keys (all optional): ``created_at``, ``valid_start_time``, ``valid_end_time``
    (ISO), ``resolved_dates`` (list of ISO), ``source``, ``origin``,
    ``provenance`` (rebuilt URL or plain label), ``tags`` (list), ``conflicts``
    (list of ``{existing_item_id?, conflict_type?, explanation?}``) and
    ``conflicts_more`` (int, when capped).
    """
    if not isinstance(item, dict):
        if item is not None:
            logger.warning(
                "Metadata summary skipped: item is %s, not a dict.", _type_name(item)
            )
        return {}
    log = _Log(item)
    try:
        out = _collect(item, log, keep)
    except Exception as exc:  # pragma: no cover - _collect guards each section
        log.drop("metadata", f"summary failed ({type(exc).__name__}); not shown")
        out = {}
    log.flush()
    return out


def summarize_item_metadata(item: Any) -> List[str]:
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
        lines.append("Resolved dates: " + ", ".join(data["resolved_dates"]))
    for key in _SOURCE_KEYS:
        if key in data:
            lines.append(f"{key.capitalize()}: {data[key]}")
    if "tags" in data:
        lines.append("Tags: " + ", ".join(data["tags"]))
    for conflict in data.get("conflicts", []):
        line = (
            f"conflicts with {conflict.get('existing_item_id', '?')}: "
            f"{conflict.get('conflict_type', 'unknown')}"
        )
        if "explanation" in conflict:
            line += f": {conflict['explanation']}"
        lines.append(line)
    if data.get("conflicts_more"):
        lines.append(f"(+{data['conflicts_more']} more conflicts)")
    return lines


# ---------------------------------------------------------------------------
# Structured items
# ---------------------------------------------------------------------------


def _score_breakdown(log: _Log) -> Callable[[Any], _Result]:
    def validate(value: Any) -> _Result:
        if not isinstance(value, dict):
            return None, f"expected a dict, got {_type_name(value)}"
        out: Dict[str, Any] = {}
        for key, raw in value.items():
            name = f"score_breakdown.{_key_name(key)}"
            if key not in _SCORE_BREAKDOWN_KEYS:
                if not _absent(raw):
                    log.omit(name)
                continue
            if _absent(raw):
                continue
            kept, note = _boolean(raw) if isinstance(raw, bool) else _number(raw)
            if kept is None:
                log.drop(name, note)
            else:
                out[key] = kept
        return out, ""

    return validate


def _top_level(log: _Log) -> Dict[str, Callable[[Any], _Result]]:
    item_id = lambda value: _ident(value, _MAX_ITEM_ID_CHARS)  # noqa: E731
    return {
        "item_id": item_id,
        "memory_type": _memory_type,
        "content": _content,
        "score": _number,
        "confidence": _number,
        "stale": _boolean,
        "superseded": _boolean,
        "reference": _boolean,
        "superseded_by": item_id,
        "derived_from": item_id,
        "as_of_resolution": _as_of,
        "score_breakdown": _score_breakdown(log),
    }


def compact_item(
    item: Any, keep_metadata_keys: Iterable[str] = ()
) -> Optional[Dict[str, Any]]:
    """Build a model-facing item from allowlisted, validated fields only.

    Top-level fields come from ``_top_level``; dates and source/origin/provenance
    copies are folded into ``metadata``; ``metadata`` is the validated summary.
    ``keep_metadata_keys`` names intentional payload keys (``effects_bundle``)
    carried verbatim. Everything else is dropped and named in a WARNING. A
    non-dict item returns ``None`` (use ``compact_items`` for lists).
    """
    if not isinstance(item, dict):
        logger.warning(
            "Metadata summary dropped an item: expected a dict, got %s.",
            _type_name(item),
        )
        return None
    keep = tuple(keep_metadata_keys)
    log = _Log(item)
    validators = _top_level(log)
    out: Dict[str, Any] = {}
    for key, raw in item.items():
        if key == "metadata" or key in _FOLDED_TOP_LEVEL:
            continue
        validate = validators.get(key)
        if validate is None:
            if not _absent(raw):
                log.omit(_key_name(key))
            continue
        if raw is None:
            out[key] = None
            continue
        value, note = validate(raw)
        if value is None:
            log.drop(key, note)
        else:
            out[key] = value
    try:
        metadata = _collect(item, log, keep)
    except Exception as exc:  # pragma: no cover - _collect guards each section
        log.drop("metadata", f"summary failed ({type(exc).__name__}); not shown")
        metadata = {}
    raw_metadata = item.get("metadata")
    if isinstance(raw_metadata, dict):
        for key in keep:
            if key in raw_metadata:
                metadata[key] = raw_metadata[key]
    out["metadata"] = metadata
    log.flush()
    return out


def compact_items(
    items: Iterable[Any], keep_metadata_keys: Iterable[str] = ()
) -> List[Dict[str, Any]]:
    """``compact_item`` over a list; non-dict entries are dropped with a WARNING."""
    keep = tuple(keep_metadata_keys)
    out: List[Dict[str, Any]] = []
    for item in items:
        compact = compact_item(item, keep)
        if compact is not None:
            out.append(compact)
    return out


def safe_memory_type(item: Any) -> str:
    """Validated memory type for prose output; ``unknown`` with a WARNING otherwise."""
    raw = item.get("memory_type") if isinstance(item, dict) else None
    if _absent(raw):
        return "unknown"
    value, note = _memory_type(raw)
    if value is None:
        _Log(item).drop("memory_type", note)
        return "unknown"
    return value
