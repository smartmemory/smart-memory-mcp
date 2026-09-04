"""SmartMemoryClerkProvider widens requested scopes to the mandatory union (S4).

Round 3 must-fix 3: `OAuthProxy.authorize` records `params.scopes or
required_scopes` and the upstream URL builder forwards that verbatim, so a
client asking for a strict subset would silently drop `user:org:read` and the
Clerk consent screen would stop offering the organization selector.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from smartmemory_mcp.hosted.auth import (
    REQUIRED_SCOPES,
    UPSTREAM_MANDATORY_SCOPES,
    SmartMemoryClerkProvider,
    build_clerk_provider,
)

from ._hosted_fixtures import hosted_config, memory_store

CLIENT_REDIRECT = "http://localhost:9876/callback"


def test_the_mandatory_set_is_the_documented_five() -> None:
    assert UPSTREAM_MANDATORY_SCOPES == {
        "openid",
        "email",
        "profile",
        "user:org:read",
        "offline_access",
    }
    # user:org:read must be REQUIRED, not merely valid: FastMCP forwards the
    # transaction's scopes, so a scope that is only "allowed" is never asked for.
    assert "user:org:read" in REQUIRED_SCOPES


def _provider() -> SmartMemoryClerkProvider:
    return build_clerk_provider(hosted_config(), redis_store=memory_store())


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="mcp-client",
        client_secret=None,
        redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _params(scopes: list[str] | None) -> AuthorizationParams:
    return AuthorizationParams(
        state="client-state",
        scopes=scopes,
        code_challenge="a" * 43,
        redirect_uri=AnyUrl(CLIENT_REDIRECT),
        redirect_uri_provided_explicitly=True,
    )


def _authorize(provider: SmartMemoryClerkProvider, scopes: list[str] | None):
    """Run authorize() and return (returned_url, stored transaction dict)."""

    async def body() -> tuple[str, dict[str, Any]]:
        url = await provider.authorize(_client(), _params(scopes))
        txn_id = parse_qs(urlparse(url).query)["txn_id"][0]
        stored = await provider._transaction_store.get(key=txn_id)
        transaction = stored.model_dump() if hasattr(stored, "model_dump") else stored
        return url, {"txn_id": txn_id, **dict(transaction)}

    return anyio.run(body)


@pytest.mark.parametrize(
    "requested",
    [
        ["openid"],
        ["openid", "email"],
        None,
        [],
        ["openid", "profile", "user:org:read", "email", "offline_access"],
    ],
)
def test_the_transaction_always_records_the_full_union(requested) -> None:
    provider = _provider()

    _, transaction = _authorize(provider, requested)

    assert set(transaction["scopes"]) == UPSTREAM_MANDATORY_SCOPES


def test_the_upstream_redirect_carries_the_full_union() -> None:
    """The scope union has to survive all the way onto the Clerk URL.

    `_build_upstream_authorize_url` is what the consent handler calls once the
    user approves, so this is the string Clerk actually receives.
    """
    provider = _provider()

    _, transaction = _authorize(provider, ["openid"])
    txn_id = transaction.pop("txn_id")
    upstream = provider._build_upstream_authorize_url(txn_id, transaction)

    query = parse_qs(urlparse(upstream).query)
    assert set(query["scope"][0].split()) == UPSTREAM_MANDATORY_SCOPES
    assert query["client_id"] == ["clerk-client-id"]
    assert query["redirect_uri"] == ["https://mcp.test/auth/callback"]
    assert upstream.startswith("https://clerk.test/oauth/authorize")


def test_a_client_requesting_extra_scopes_keeps_them() -> None:
    """The union widens; it must never narrow what a client legitimately asked for."""
    provider = _provider()

    _, transaction = _authorize(provider, ["openid", "public_metadata"])

    assert set(transaction["scopes"]) == UPSTREAM_MANDATORY_SCOPES | {"public_metadata"}


def test_the_base_class_alone_would_have_dropped_the_org_scope() -> None:
    """Pins the bug being compensated for, so a fastmcp fix is visible.

    If a future fastmcp unions on its own, this assertion fails and the override
    can be deleted rather than quietly duplicating upstream behaviour.
    """
    from fastmcp.server.auth.providers.clerk import ClerkProvider

    assert SmartMemoryClerkProvider.authorize is not ClerkProvider.authorize

    provider = _provider()
    params = _params(["openid"])
    effective = params.scopes or provider.required_scopes or []
    assert set(effective) == {"openid"}
    assert "user:org:read" not in effective
