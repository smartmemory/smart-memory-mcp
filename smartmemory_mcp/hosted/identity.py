"""Per-request hosted identity (PLAT-MCP-HOSTED-1 S1).

Two types, deliberately distinct (design.md §4, round 3 should-fix 3):

* `VerifiedIdentity` — the cacheable result of an OAuth exchange or an API-key
  verification. It holds no backend, so it can be shared across calls.
* `HostedIdentity` — request-scoped. Built once per tool call by the middleware
  AFTER the session team has been read, so its `backend` already carries the
  right `X-Workspace-Id`. Dispatch returns THAT instance and never rebuilds one.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .backend import HostedRemoteBackend
    from .exchange import ExchangeCache


class HostedAuthError(Exception):
    """A post-transport auth failure.

    Surfaced to the MCP client as a tool error prefixed `Unauthorized:`
    (contract `auth_failure_envelope`), never as an HTTP challenge.
    """


@dataclass(frozen=True)
class VerifiedIdentity:
    """Cacheable identity: who the caller is and the SmartMemory token to use."""

    kind: Literal["oauth", "api_key"]
    subject: str
    sm_access_token: str
    sm_expires_at: datetime
    user_id: str
    tenant_id: str
    email: str
    default_team_id: str | None
    active_workspace_id: str | None
    fingerprint: str


@dataclass
class HostedIdentity:
    """Request-scoped identity. `backend` is required, never rebuilt downstream."""

    verified: VerifiedIdentity
    team_id: str
    backend: HostedRemoteBackend


# Set by HostedIdentityMiddleware for the duration of one tool call and reset in
# `finally`. A tool that reaches get_backend() with this unset must raise
# HostedAuthError rather than fall through to the singleton or to env credentials.
current_identity: ContextVar[HostedIdentity | None] = ContextVar(
    "smartmemory_mcp_current_identity", default=None
)

# Process-wide: True only while the hosted server is running. It is what turns an
# unbound contextvar from "local/stdio, use the singleton" into a hard failure —
# without it a hosted tool call that somehow bypassed the middleware would fall
# through to whatever SMARTMEMORY_API_KEY the container happens to carry and act
# as the wrong tenant.
_hosted_mode = False


def set_hosted_mode(enabled: bool) -> None:
    """Enable/disable hosted mode. Called once by the hosted server builder."""
    global _hosted_mode
    _hosted_mode = enabled


def hosted_mode_enabled() -> bool:
    """True when this process is serving the hosted, multi-tenant endpoint."""
    return _hosted_mode


def resolve_hosted_backend() -> HostedRemoteBackend | None:
    """The per-call hosted backend, or None when this is not a hosted call.

    Returns the exact instance the middleware built for this tool call — never a
    rebuild, which would lose the session-selected team (round 2 finding 4).
    Raises HostedAuthError in hosted mode with nothing bound: there is no safe
    fallback for an unidentified caller on a multi-tenant server.
    """
    identity = current_identity.get()
    if identity is not None:
        return identity.backend
    if _hosted_mode:
        raise HostedAuthError("no identity bound to this call")
    return None


def build_hosted_backend(
    verified: VerifiedIdentity,
    team_id: str | None = None,
    *,
    api_url: str,
    cache: ExchangeCache | None = None,
) -> HostedRemoteBackend:
    """Construct the per-call backend STRICTLY.

    Every credential is passed explicitly and asserted non-empty, so the
    RemoteBackend constructor's environment fallbacks (`remote.py:28-36`) can
    never engage in hosted mode. `_bootstrapped` is pre-set so the `/auth/me`
    bootstrap never runs against another tenant's default team.
    """
    from .backend import HostedRemoteBackend
    from .exchange import DEFAULT_EXCHANGE_CACHE

    resolved_team = team_id or verified.active_workspace_id or ""

    assert api_url, "hosted backend requires an explicit api_url"
    assert verified.sm_access_token, "hosted backend requires a SmartMemory token"
    assert resolved_team, "hosted backend requires a workspace id"
    assert verified.fingerprint, "hosted backend requires a token fingerprint"

    backend = HostedRemoteBackend(
        api_url=api_url,
        api_key=verified.sm_access_token,
        team_id=resolved_team,
        fingerprint=verified.fingerprint,
        cache=DEFAULT_EXCHANGE_CACHE if cache is None else cache,
    )
    backend._session.update(
        {
            "_bootstrapped": True,
            "user_email": verified.email,
            "team_id": resolved_team,
        }
    )
    return backend
