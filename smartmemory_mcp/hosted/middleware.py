"""HostedIdentityMiddleware — the one async boundary of hosted mode (S3).

design.md §4: a FastMCP middleware hook runs before every tool call, resolves
who is calling, builds that caller's backend AFTER reading the session team, and
binds it to a contextvar for the duration of the call. Tools stay synchronous
and only ever read the contextvar.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

import anyio.to_thread
import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import TextContent

from .backend import HostedNdaRequiredError
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

# MCP-FIRST-RUN-HINT-1: the first successful tool result of each MCP session carries this once. It never
# mentions local mode: the remote server is the product here, the CLI is an extra.
FIRST_RUN_HINT = "`pip install smartmemory` for the sm CLI"
# WS-ROUTING-4: the workspace selection lives server-side in `routing_sessions`, keyed by (tenant, user, client_key,
# context_key), not by MCP session id, so a reconnect or an MCP process restart keeps it. The hosted client key is
# `mcp:<clientInfo.name>` (contract client_key.grammar); a client that sends no name shares `mcp:hosted`.
SESSION_CLIENT_FALLBACK = "mcp:hosted"
_CLIENT_NAME_DROP = re.compile(r"[^A-Za-z0-9 _.\-]")
ROUTING_SESSION_PATH = "/memory/routing/session"
ROUTING_EXPLAIN_PATH = "/memory/routing/explain"


class HostedSessionSelectionError(RuntimeError):
    """svc-api could not read or save the caller's workspace selection; the message is safe to show."""


# Sessions already hinted, oldest first. Bounded so a long-lived process cannot grow it forever; a session that
# falls off the end is hinted once more, which is harmless.
HINTED_SESSIONS_MAX = 4096


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
        self._web_url = config.web_url
        self._cache = DEFAULT_EXCHANGE_CACHE if cache is None else cache
        self._transport = transport
        self._hinted_sessions: OrderedDict[str, None] = OrderedDict()

    def _unauthorized(self, reason: object) -> ToolError:
        """An auth refusal that says what to do next, not just that it happened."""
        return ToolError(
            f"Unauthorized: {reason}. Sign in with your MCP client's OAuth prompt, or send a "
            f"SmartMemory API key as `Authorization: Bearer <key>` (create one in the web app at "
            f"{self._web_url})."
        )

    def _claim_first_run_hint(self, fastmcp_context: Any) -> bool:
        """True exactly once per MCP session. No session id (no transport session) means no hint."""
        session_id = _mcp_session_id(fastmcp_context)
        if session_id is None or session_id in self._hinted_sessions:
            return False
        self._hinted_sessions[session_id] = None
        while len(self._hinted_sessions) > HINTED_SESSIONS_MAX:
            self._hinted_sessions.popitem(last=False)
        return True

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        access_token = get_access_token()
        if access_token is None:
            # Transport-level auth should have caught this; if it ever does not,
            # fail closed rather than serve an unidentified caller.
            raise self._unauthorized("no access token on this request")

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
            raise self._unauthorized(exc) from exc

        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            raise ToolError(
                "Hosted identity requires an MCP request context; none was attached."
            )

        # The session team must be read BEFORE the backend is built, or the call
        # goes out with the wrong X-Workspace-Id (round 3 must-fix 1).
        try:
            session_team = await self.get_session_team(fastmcp_context, verified)
        except HostedAuthError as exc:
            raise self._unauthorized(exc) from exc
        except HostedSessionSelectionError as exc:
            # Fail closed: guessing the default here could send a write to a workspace the person switched away from.
            raise ToolError(str(exc)) from exc
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
            result = await call_next(context)
            # Claimed after the tool succeeded, so a call that errors leaves the hint for the next one.
            if hasattr(result, "content") and self._claim_first_run_hint(fastmcp_context):
                result.content = [*result.content, TextContent(type="text", text=FIRST_RUN_HINT)]
            return result
        except Exception as exc:
            if _hosted_nda_required_cause(exc) is not None:
                raise ToolError(
                    "Accept the beta agreement in the SmartMemory web app at "
                    f"{self._web_url}."
                ) from exc
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
            raise self._unauthorized(auth_error) from exc
        finally:
            current_identity.reset(token)

    def _exchange(self, upstream_token: str) -> VerifiedIdentity:
        """Cache-or-exchange, run off the event loop (httpx.Client is sync)."""
        with httpx.Client(transport=self._transport, timeout=15.0) as client:
            return get_sm_identity(
                self._cache, self._api_url, upstream_token, client=client
            )

    async def get_session_team(
        self, context: Any, verified: VerifiedIdentity | None = None
    ) -> str | None:
        """The caller's saved workspace selection for this MCP client, read from svc-api.

        `GET /memory/routing/explain?client_key=mcp:<name>` with no workspace header: the `session` candidate when the
        server chose it, else None (no selection, expired, lost access, or a workspace-bound key). Raises
        `HostedAuthError` on a 401 and `HostedSessionSelectionError` when svc-api cannot answer.
        """
        verified = verified or _bound_verified()
        if verified is None:
            return None
        return await anyio.to_thread.run_sync(
            self._read_selection, verified, mcp_client_key(context)
        )

    async def set_session_team(self, context: Any, team_id: str) -> None:
        """Save a workspace selection for this caller and MCP client (`PUT /memory/routing/session`).

        svc-api re-runs the membership check and refuses a workspace-bound key; a refusal raises
        `HostedSessionSelectionError` and nothing changes.
        """
        verified = _bound_verified()
        if verified is None:
            raise HostedSessionSelectionError(
                "no hosted identity is bound to this call; the selection was not saved."
            )
        await anyio.to_thread.run_sync(
            self._save_selection, verified, mcp_client_key(context), team_id
        )

    def _routing_call(self, verified: VerifiedIdentity, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {verified.sm_access_token}"}
        try:
            with httpx.Client(transport=self._transport, timeout=15.0) as client:
                response = client.request(method, f"{self._api_url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning(
                "Workspace selection call failed (lost=session selection, error=%s)",
                type(exc).__name__,
            )
            raise HostedSessionSelectionError(
                "could not reach SmartMemory to read your workspace selection; retry."
            ) from exc
        if response.status_code == 401:
            logger.warning(
                "svc-api rejected the hosted session on a workspace-selection call (401); "
                "invalidating the cached exchange for fingerprint %s.",
                verified.fingerprint[:12],
            )
            self._cache.invalidate(verified.fingerprint)
            raise HostedAuthError("session expired, retry")
        return response

    def _read_selection(self, verified: VerifiedIdentity, client_key: str) -> str | None:
        response = self._routing_call(
            verified, "GET", ROUTING_EXPLAIN_PATH, params={"client_key": client_key}
        )
        if response.status_code == 404:
            logger.warning(
                "svc-api has no routing explain route; using the default workspace (lost=session selection)"
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "Workspace selection read refused (status=%s, lost=session selection)",
                response.status_code,
            )
            raise HostedSessionSelectionError(
                f"could not read your workspace selection (HTTP {response.status_code}); retry."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise HostedSessionSelectionError(
                "could not read your workspace selection (unexpected response); retry."
            ) from exc
        candidates = body.get("candidates") if isinstance(body, dict) else None
        for candidate in candidates if isinstance(candidates, list) else []:
            if (
                isinstance(candidate, dict)
                and candidate.get("lever") == "session"
                and candidate.get("status") == "chosen"
                and candidate.get("workspace_id")
            ):
                return str(candidate["workspace_id"])
        return None

    def _save_selection(self, verified: VerifiedIdentity, client_key: str, team_id: str) -> None:
        response = self._routing_call(
            verified,
            "PUT",
            ROUTING_SESSION_PATH,
            json={"workspace_id": team_id, "client_key": client_key},
        )
        if response.status_code == 200:
            return
        logger.warning(
            "Workspace selection save refused (status=%s, lost=switch)", response.status_code
        )
        reason = {
            403: "you are not a member of that workspace",
            409: "this API key is bound to one workspace and cannot switch",
        }.get(response.status_code, f"HTTP {response.status_code}")
        raise HostedSessionSelectionError(f"could not save the switch ({reason}).")


def mcp_client_key(context: Any) -> str:
    """`mcp:<clientInfo.name>` from the MCP `initialize` handshake, cut to the contract grammar."""
    try:
        params = context.session.client_params
        # The MCP SDK model field is `client_info` (wire name `clientInfo`); older SDKs exposed the camelCase name.
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        name = getattr(info, "name", "") if info is not None else ""
    except (AttributeError, RuntimeError):
        name = ""
    name = _CLIENT_NAME_DROP.sub("", str(name or "")).strip()[:64].strip()
    return f"mcp:{name}" if name else SESSION_CLIENT_FALLBACK


def _bound_verified() -> VerifiedIdentity | None:
    identity = current_identity.get()
    return identity.verified if identity is not None else None


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


def _mcp_session_id(context: Any) -> str | None:
    """Return a session id when the FastMCP transport has established one."""
    try:
        session_id = context.session_id
    except (AttributeError, RuntimeError):
        return None
    return str(session_id) if session_id else None


def _hosted_nda_required_cause(exc: BaseException) -> HostedNdaRequiredError | None:
    """Find the beta-gate marker through FastMCP's wrapped tool failures."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, HostedNdaRequiredError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _identity_from_api_key(access_token: Any) -> VerifiedIdentity:
    """An API key IS the SmartMemory credential — no exchange, no cache entry."""
    claims = access_token.claims or {}
    subject = str(claims.get("sub") or "")
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
