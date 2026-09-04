"""SmartMemory API-key token verifier for the hosted server (S2).

The prefix regex is only a cheap pre-filter that avoids spending a network call
on an obvious non-key (a Clerk `oat_` token, a JWT). Every key that passes it is
validated by svc-api; nothing is accepted locally. Legacy `sk_` keys are still
honoured service-side (`jwt_provider.py:86-89`), so they match too (round 4 S1).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

import httpx
from fastmcp.server.auth.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

API_KEY_PATTERN = re.compile(r"^(sm_(live|test)_|sk_)[A-Za-z0-9_-]{16,}$")
CACHE_TTL_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 10.0


class SmartMemoryApiKeyVerifier(TokenVerifier):
    """Verify `sm_live_` / `sm_test_` / legacy `sk_` keys against `/auth/me`."""

    def __init__(
        self,
        api_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl: float = CACHE_TTL_SECONDS,
        base_url: str | None = None,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(base_url=base_url, required_scopes=required_scopes)
        self._api_url = api_url.rstrip("/")
        self._transport = transport
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, AccessToken]] = {}
        self._clock = time.monotonic

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not API_KEY_PATTERN.match(token):
            # Not an API key at all — no network call. The OAuth verifier in the
            # MultiAuth chain gets its turn.
            return None

        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._cache.get(fingerprint)
        if cached is not None:
            expires_at, access_token = cached
            if self._clock() < expires_at:
                return access_token
            self._cache.pop(fingerprint, None)

        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=REQUEST_TIMEOUT_SECONDS
            ) as client:
                response = await client.get(
                    f"{self._api_url}/auth/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "API-key verification could not reach %s/auth/me: %s",
                self._api_url,
                exc,
            )
            return None

        if response.status_code != 200:
            logger.warning(
                "API-key rejected by svc-api: HTTP %s (fingerprint %s).",
                response.status_code,
                fingerprint[:12],
            )
            return None

        try:
            user = response.json()
        except ValueError:
            logger.warning("API-key verification got a non-JSON /auth/me body.")
            return None
        if not isinstance(user, dict) or not user.get("id"):
            logger.warning("API-key verification got a /auth/me body with no user id.")
            return None

        default_team_id = user.get("default_team_id") or None
        access_token = AccessToken(
            token=token,
            client_id="api-key",
            # /auth/me carries no scopes field; authorization stays service-enforced
            # on the forwarded key (round 4 N2).
            scopes=[],
            subject=str(user["id"]),
            claims={
                "kind": "api_key",
                "tenant_id": user.get("tenant_id"),
                "default_team_id": default_team_id,
                # No org context on an API key: the personal default team is the
                # workspace the hosted session starts in.
                "active_workspace_id": default_team_id,
                "email": user.get("email"),
            },
            expires_at=None,
        )
        self._cache[fingerprint] = (self._clock() + self._cache_ttl, access_token)
        logger.info(
            "API-key accepted for user %s (fingerprint %s).",
            access_token.subject,
            fingerprint[:12],
        )
        return access_token
