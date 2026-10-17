"""Guard for caller- or model-supplied ids placed in a request path.

The service runs uvicorn, which unquotes the raw path before Starlette routes it, so
`%2F` is a real `/` by the time a route is chosen: percent-encoding alone does not keep
an id inside its own route. `memory_delete("teams/T1")` would reach
`DELETE /memory/teams/{team_id}`. And a plain id equal to a static sibling route needs
no special character at all: `memory_delete("clear-all")` is `DELETE /memory/clear-all`.

So `path_id` refuses, before any request is sent, every id that could leave its route:
anything with `/` or `\\`, empty, `.`, `..`, control characters, and any id equal to a
static path segment the service has at that position. Everything else is
percent-encoded as one segment (`?`, `#`, `%`, spaces), so a normal id is unchanged.
"""

from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import quote

# Static path segments the service has directly under each prefix we put an id after,
# for ANY method (a superset of the per-method siblings). Derived from the service
# OpenAPI snapshot
# (smart-memory-service/tests/contracts/snapshots/openapi_schema.json, 2026-10-17);
# tests/test_path_id_encoding.py fails when the snapshot has a segment missing here.
# The package cannot read the service repo at runtime, so the list is checked in.
RESERVED_SEGMENTS: dict[str, frozenset[str]] = {
    "/memory/": frozenset(
        """
        adapters add admin agents analytics anchors api-keys archive ask audit beta
        by-metadata clear clear-all clustering code context decisions edge entities
        evolution failures feedback governance graph health idp-drift import inference
        ingest launch licensing link list lite locks logs maintenance okf ontology
        opinions patterns personalize pipeline plans platform plugins policy procedures
        progress read-around reasoning recall reflect result-feedback schemas search
        sequences sleep subscriptions summary sync teams telemetry temporal token-usage
        validate worker workspaces zettel
        """.split()
    ),
    "/memory/decisions/": frozenset({"create", "pending", "search"}),
    "/memory/decisions/pending/": frozenset({"create"}),
    "/memory/agents/": frozenset(),
}


def path_id(value: Any, *, under: str) -> str:
    """Return `value` as ONE safe path segment to follow the prefix `under`.

    Raises ValueError (no request is made) when the id could reach another route.
    `under` is the literal path text before the id, e.g. "/memory/decisions/".
    """
    try:
        reserved = RESERVED_SEGMENTS[under]
    except KeyError:
        raise LookupError(
            f"No reserved-segment list for prefix {under!r}: add it to "
            "RESERVED_SEGMENTS in smartmemory_mcp/backends/path_guard.py"
        ) from None
    text = "" if value is None else str(value)
    if text in ("", ".", ".."):
        raise ValueError(f"Invalid id {text!r}: it must name one item, not a path")
    if "/" in text or "\\" in text:
        raise ValueError(f"Invalid id {text!r}: an id cannot contain '/' or '\\'")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise ValueError(
            f"Invalid id {text!r}: an id cannot contain control characters"
        )
    if text in reserved:
        raise ValueError(
            f"Invalid id {text!r}: it is the name of another route under {under}"
        )
    return quote(text, safe="")
