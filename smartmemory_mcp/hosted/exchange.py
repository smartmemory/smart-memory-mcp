"""Clerk OAuth token -> SmartMemory JWT exchange and its cache (S2).

One `POST /auth/clerk/oauth-exchange` per upstream token, cached until 60 s
before the minted token expires (contract `mcp_server_client_behaviour`).
The upstream token itself is never logged and never appears in an exception.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .identity import HostedAuthError, VerifiedIdentity

logger = logging.getLogger(__name__)

EXCHANGE_PATH = "/auth/clerk/oauth-exchange"
CACHE_SKEW_SECONDS = 60
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_TIMEOUT_SECONDS = 15.0


def token_fingerprint(token: str) -> str:
    """sha256 hex of the upstream token — the cache key and the log-safe id."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ExchangeCache:
    """Bounded LRU of verified identities keyed by upstream-token fingerprint."""

    def __init__(self, max_size: int = DEFAULT_MAX_ENTRIES) -> None:
        self._max_size = max_size
        self._entries: OrderedDict[str, VerifiedIdentity] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, fingerprint: object) -> bool:
        return fingerprint in self._entries

    def get(self, fingerprint: str) -> VerifiedIdentity | None:
        """Return a live entry, evicting it once it is within the skew of expiry."""
        identity = self._entries.get(fingerprint)
        if identity is None:
            return None
        deadline = identity.sm_expires_at - timedelta(seconds=CACHE_SKEW_SECONDS)
        if _now() >= deadline:
            self._entries.pop(fingerprint, None)
            return None
        self._entries.move_to_end(fingerprint)
        return identity

    def put(self, fingerprint: str, identity: VerifiedIdentity) -> None:
        self._entries[fingerprint] = identity
        self._entries.move_to_end(fingerprint)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)

    def invalidate(self, fingerprint: str) -> None:
        """Drop one entry. Unknown fingerprints are a no-op, never an error."""
        self._entries.pop(fingerprint, None)


# Process-wide cache used by the hosted middleware and by HostedRemoteBackend
# invalidation. Tests construct their own.
DEFAULT_EXCHANGE_CACHE = ExchangeCache()


def exchange(
    api_url: str,
    upstream_token: str,
    *,
    client: httpx.Client | None = None,
) -> VerifiedIdentity:
    """POST the upstream Clerk token to svc-api and return the verified identity.

    Raises HostedAuthError on any non-200 or transport failure, logged at
    WARNING with the status and the service's error code — never the token.
    """
    url = f"{api_url.rstrip('/')}{EXCHANGE_PATH}"
    owned = client is None
    http = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        response = http.post(url, json={"token": upstream_token})
    except httpx.HTTPError as exc:
        logger.warning("OAuth exchange to %s failed to complete: %s", url, exc)
        raise HostedAuthError(f"identity exchange unreachable at {url}") from exc
    finally:
        if owned:
            http.close()

    if response.status_code != 200:
        code = _error_code(response)
        logger.warning(
            "OAuth exchange rejected: HTTP %s (%s)", response.status_code, code
        )
        raise HostedAuthError(
            f"identity exchange failed: HTTP {response.status_code} ({code})"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("OAuth exchange returned a non-JSON body (HTTP 200).")
        raise HostedAuthError(
            "identity exchange returned a malformed response"
        ) from exc

    return _to_identity(payload, token_fingerprint(upstream_token))


def get_sm_identity(
    cache: ExchangeCache,
    api_url: str,
    upstream_token: str,
    *,
    client: httpx.Client | None = None,
) -> VerifiedIdentity:
    """Cache-or-exchange. One network call per upstream token per TTL."""
    fingerprint = token_fingerprint(upstream_token)
    cached = cache.get(fingerprint)
    if cached is not None:
        return cached
    identity = exchange(api_url, upstream_token, client=client)
    cache.put(fingerprint, identity)
    return identity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unknown_error"
    if isinstance(body, dict):
        for key in ("error", "detail", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("error")
                if isinstance(nested, str) and nested:
                    return nested
    return "unknown_error"


def _to_identity(payload: Any, fingerprint: str) -> VerifiedIdentity:
    if not isinstance(payload, dict):
        raise HostedAuthError("identity exchange returned a malformed response")
    try:
        access_token = payload["access_token"]
        expires_at = _parse_expiry(payload["expires_at"])
        user_id = str(payload["user_id"])
        tenant_id = str(payload["tenant_id"])
        email = str(payload["email"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HostedAuthError(
            f"identity exchange response is missing required fields: {exc}"
        ) from exc

    if not isinstance(access_token, str) or not access_token:
        raise HostedAuthError("identity exchange returned no access token")

    default_team_id = payload.get("default_team_id") or None
    active_workspace_id = payload.get("active_workspace_id") or default_team_id

    return VerifiedIdentity(
        kind="oauth",
        subject=user_id,
        sm_access_token=access_token,
        sm_expires_at=expires_at,
        user_id=user_id,
        tenant_id=tenant_id,
        email=email,
        default_team_id=default_team_id,
        active_workspace_id=active_workspace_id,
        fingerprint=fingerprint,
    )


def _parse_expiry(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        parsed = raw
    else:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
