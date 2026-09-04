"""HostedIdentityMiddleware — the one async boundary of hosted mode (S3).

design.md §4: a FastMCP middleware hook runs before every tool call, resolves
who is calling, builds that caller's backend AFTER reading the session team, and
binds it to a contextvar for the duration of the call. Tools stay synchronous
and only ever read the contextvar.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import anyio.to_thread
import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from .config import HostedConfig
from .exchange import DEFAULT_EXCHANGE_CACHE, ExchangeCache, get_sm_identity
from .identity import (
    HostedAuthError,
    HostedIdentity,
    VerifiedIdentity,
    build_hosted_backend,
    current_identity,
)

logger = logging.getLogger(__name__)

# API keys do not expire on their own; the verifier's own 60 s cache governs how
# often svc-api re-checks one. This value only has to sit far past any single call.
API_KEY_NO_EXPIRY = datetime(9999, 12, 31, tzinfo=timezone.utc)


class HostedIdentityMiddleware(Middleware):
    """Bind a per-call `HostedIdentity` around every tool invocation."""

    def __init__(
        self,
        config: HostedConfig,
        *,
        cache: ExchangeCache | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_url = config.api_url
        self._cache = DEFAULT_EXCHANGE_CACHE if cache is None else cache
        self._transport = transport

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        access_token = get_access_token()
        if access_token is None:
            # Transport-level auth should have caught this; if it ever does not,
            # fail closed rather than serve an unidentified caller.
            raise ToolError("Unauthorized: no access token on this request")

        claims = access_token.claims or {}
        # Classify by the verifier's own claim, never by re-matching the prefix:
        # a legacy `sk_` key IS an API key and must not be sent to the OAuth
        # exchange (round 5 should-fix 1).
        kind = "api_key" if claims.get("kind") == "api_key" else "oauth"

        try:
            if kind == "api_key":
                verified = _identity_from_api_key(access_token)
            else:
                verified = await anyio.to_thread.run_sync(
                    self._exchange, access_token.token
                )
        except HostedAuthError as exc:
            logger.warning("Hosted identity rejected (%s): %s", kind, exc)
            raise ToolError(f"Unauthorized: {exc}") from exc

        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            raise ToolError(
                "Hosted identity requires an MCP request context; none was attached."
            )

        # The session team must be read BEFORE the backend is built, or the call
        # goes out with the wrong X-Workspace-Id (round 3 must-fix 1).
        session_team = await fastmcp_context.get_state("team_id")
        team_id = session_team or verified.active_workspace_id

        try:
            backend = build_hosted_backend(
                verified, team_id, api_url=self._api_url, cache=self._cache
            )
        except AssertionError as exc:
            logger.warning("Hosted backend construction refused: %s", exc)
            raise ToolError(f"Unauthorized: incomplete identity ({exc})") from exc

        identity = HostedIdentity(
            verified=verified,
            team_id=str(backend._session["team_id"]),
            backend=backend,
        )
        token = current_identity.set(identity)
        try:
            return await call_next(context)
        except Exception as exc:
            # A svc-api 401 during the call. FastMCP's tool runner has usually
            # already wrapped it as `ToolError("Error calling tool ...")`
            # (server.py:1541-1555), so the HostedAuthError arrives as the cause
            # rather than as the exception itself — and under masked error
            # details it is the ONLY place the reason survives. Either way the
            # cache entry is gone and the client's next call re-exchanges: one
            # retry, no hidden loop.
            auth_error = _hosted_auth_cause(exc)
            if auth_error is None:
                raise
            logger.warning("Hosted call lost its authorization: %s", auth_error)
            raise ToolError(f"Unauthorized: {auth_error}") from exc
        finally:
            current_identity.reset(token)

    def _exchange(self, upstream_token: str) -> VerifiedIdentity:
        """Cache-or-exchange, run off the event loop (httpx.Client is sync)."""
        with httpx.Client(transport=self._transport, timeout=15.0) as client:
            return get_sm_identity(
                self._cache, self._api_url, upstream_token, client=client
            )


def _hosted_auth_cause(exc: BaseException) -> HostedAuthError | None:
    """Find a HostedAuthError anywhere in an exception's cause chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, HostedAuthError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _identity_from_api_key(access_token: Any) -> VerifiedIdentity:
    """An API key IS the SmartMemory credential — no exchange, no cache entry."""
    claims = access_token.claims or {}
    subject = access_token.subject or ""
    if not subject:
        raise HostedAuthError("verified API key carries no subject")
    default_team_id = claims.get("default_team_id") or None
    return VerifiedIdentity(
        kind="api_key",
        subject=subject,
        sm_access_token=access_token.token,
        sm_expires_at=API_KEY_NO_EXPIRY,
        user_id=subject,
        tenant_id=str(claims.get("tenant_id") or ""),
        email=str(claims.get("email") or ""),
        default_team_id=default_team_id,
        active_workspace_id=claims.get("active_workspace_id") or default_team_id,
        fingerprint=hashlib.sha256(access_token.token.encode("utf-8")).hexdigest(),
    )
