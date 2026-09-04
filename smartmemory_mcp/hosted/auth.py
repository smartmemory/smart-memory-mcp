"""Auth provider construction for the hosted server (S4, design.md §2)."""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet
from fastmcp.server.auth.auth import MultiAuth
from fastmcp.server.auth.providers.clerk import ClerkProvider
from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.wrappers.encryption.fernet import FernetEncryptionWrapper
from key_value.aio.wrappers.limit_size.wrapper import LimitSizeWrapper
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from .api_key_verifier import SmartMemoryApiKeyVerifier
from .config import HostedConfig
from .storage import DEFAULT_CLIENT_RECORD_TTL, DefaultTTLWrapper

logger = logging.getLogger(__name__)

# Every scope Clerk must be ASKED for on each authorization, not merely one the
# client is allowed to request. `user:org:read` is what puts the organization
# selector on Clerk's consent screen and what makes `/oauth/userinfo` return
# `org_id`; `offline_access` is what gets a refresh token at all.
UPSTREAM_MANDATORY_SCOPES = frozenset(
    {"openid", "email", "profile", "user:org:read", "offline_access"}
)

REQUIRED_SCOPES = ["openid", "email", "profile", "user:org:read"]
VALID_SCOPES = [*REQUIRED_SCOPES, "offline_access"]

REDIRECT_PATH = "/auth/callback"
MAX_STORED_RECORD_BYTES = 64 * 1024


class SmartMemoryClerkProvider(ClerkProvider):
    """ClerkProvider that UNIONS the requested scopes with the mandatory set.

    `OAuthProxy.authorize` records `params.scopes or self.required_scopes`
    (`oauth_proxy/proxy.py:1190`) and `_build_upstream_authorize_url` forwards
    that verbatim (`:891`) — an `or`, never a union. So a client asking only for
    `scope=openid` would suppress `email profile user:org:read`: the org
    selector would vanish from the consent screen and Clerk's own verifier would
    then reject the resulting token for missing required scopes
    (`providers/clerk.py:169-185`). Round 3, must-fix 3.
    """

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        requested = set(params.scopes or [])
        union = sorted(requested | UPSTREAM_MANDATORY_SCOPES)
        if union != sorted(requested):
            logger.debug(
                "Widening requested scopes %s to the upstream mandatory union %s.",
                sorted(requested),
                union,
            )
        return await super().authorize(
            client, params.model_copy(update={"scopes": union})
        )


def build_client_storage(
    cfg: HostedConfig, *, redis_store: AsyncKeyValue | None = None
) -> AsyncKeyValue:
    """Encrypted, size-capped, TTL-defaulted storage for all OAuth state.

    A bare store would hold upstream Clerk tokens in plaintext
    (`oauth_proxy/proxy.py:584,609,1406`), so the Fernet wrapper is the outermost
    layer and everything below it only ever sees the envelope.
    """
    if redis_store is None:
        # Imported lazily so a caller that injects a store never needs redis.
        from key_value.aio.stores.redis import RedisStore

        redis_store = RedisStore(url=cfg.redis_url)

    return FernetEncryptionWrapper(
        key_value=LimitSizeWrapper(
            DefaultTTLWrapper(redis_store, default_ttl=DEFAULT_CLIENT_RECORD_TTL),
            max_size=MAX_STORED_RECORD_BYTES,
        ),
        fernet=Fernet(cfg.state_encryption_key),
        raise_on_decryption_error=False,
    )


def build_clerk_provider(
    cfg: HostedConfig, *, redis_store: AsyncKeyValue | None = None
) -> SmartMemoryClerkProvider:
    """The upstream OAuth half of the hosted auth chain."""
    return SmartMemoryClerkProvider(
        domain=cfg.clerk_domain,
        client_id=cfg.clerk_oauth_client_id,
        client_secret=cfg.clerk_oauth_client_secret,
        base_url=cfg.public_base_url,
        redirect_path=REDIRECT_PATH,
        required_scopes=list(REQUIRED_SCOPES),
        valid_scopes=list(VALID_SCOPES),
        enable_cimd=True,
        allowed_client_redirect_uris=list(cfg.allowed_client_redirects),
        client_storage=build_client_storage(cfg, redis_store=redis_store),
        # Passed explicitly: left unset, FastMCP derives the signing key from the
        # Clerk client secret (`oauth_proxy/proxy.py:557-565`), which would make
        # rotating that secret silently invalidate every issued token.
        jwt_signing_key=cfg.jwt_signing_key,
        require_authorization_consent=True,
        # Clerk may omit refresh_expires_in; without this the stored state would
        # outlive or underlive the refresh JWT FastMCP issues alongside it.
        fallback_refresh_token_expiry_seconds=DEFAULT_CLIENT_RECORD_TTL,
    )


def build_auth(
    cfg: HostedConfig, *, redis_store: AsyncKeyValue | None = None
) -> MultiAuth:
    """The full hosted auth chain: Clerk OAuth first, then SmartMemory API keys.

    `required_scopes=[]` at the MultiAuth level is deliberate: the OIDC scopes
    are enforced inside ClerkProvider, and an API key carries SmartMemory
    scopes rather than OIDC ones, so a global requirement would reject it after
    it had already verified successfully (round 1, finding 3).
    """
    return MultiAuth(
        server=build_clerk_provider(cfg, redis_store=redis_store),
        verifiers=[SmartMemoryApiKeyVerifier(api_url=cfg.api_url)],
        required_scopes=[],
    )
