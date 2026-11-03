"""Workspace selection shared by the stdio and hosted switch tools (MCP-SWITCH-WORKSPACE-1).

A workspace is a box inside a tenant. The tenant comes from the credential and is
never a tool argument (MCP-TOOL-SURFACE-1 section 4: no `switch_tenant`, no `tenant`
parameter before WS-POLICY-17). Switching changes only which of the caller's own
workspaces the next request names in `X-Workspace-Id`; the service still checks
membership on every request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEPRECATED_ALIAS = "switch_team"
ALIAS_NOTICE = (
    "switch_team is deprecated and will be removed in the next release: "
    "use switch_workspace(workspace)."
)

_alias_warned = False


@dataclass(frozen=True)
class SwitchResult:
    """Outcome of a switch. `ok=False` means the previous workspace is still active."""

    ok: bool
    message: str
    workspace_id: str = ""


def warn_alias_once() -> None:
    """Log the deprecation WARNING for `switch_team` once per process."""
    global _alias_warned
    if _alias_warned:
        return
    _alias_warned = True
    logger.warning("%s (MCP-SWITCH-WORKSPACE-1)", ALIAS_NOTICE)


def reset_alias_warning() -> None:
    """Re-arm the once-per-process WARNING. Tests only."""
    global _alias_warned
    _alias_warned = False


def resolve_workspace(
    rows: list[dict[str, Any]], ref: str, *, id_key: str
) -> tuple[str | None, str]:
    """Match `ref` to one of `rows` by id, then by name (case-insensitive).

    Returns `(id, label)` on a match, `(None, refusal text)` otherwise. Only ids in
    `rows` can come back, so a caller can never select a workspace the listing did
    not grant.
    """
    ref = ref.strip()
    for row in rows:
        if str(row.get(id_key)) == ref:
            return ref, _label(row, id_key)
    by_name = [r for r in rows if str(r.get("name") or "").casefold() == ref.casefold()]
    if len(by_name) == 1:
        return str(by_name[0][id_key]), _label(by_name[0], id_key)
    if len(by_name) > 1:
        ids = ", ".join(sorted(str(r[id_key]) for r in by_name))
        return (
            None,
            f"Workspace name {ref!r} matches more than one workspace; use an id: {ids}.",
        )
    available = ", ".join(sorted(_label(r, id_key) for r in rows)) or "none"
    return None, f"You are not a member of workspace {ref!r}. Available: {available}."


def refuse(reason: str, previous: str) -> SwitchResult:
    """A failed switch: one WARNING, and a message naming the workspace still in use."""
    logger.warning("Workspace switch refused: %s Still using %r.", reason, previous)
    still = previous or "(none: the service default)"
    return SwitchResult(
        ok=False,
        message=f"Workspace not switched: {reason} Still using workspace: {still}.",
    )


def _label(row: dict[str, Any], id_key: str) -> str:
    name = row.get("name")
    wid = str(row.get(id_key))
    return f"{name} ({wid})" if name and name != wid else wid
