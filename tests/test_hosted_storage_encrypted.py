"""OAuth state is encrypted at rest (S4, design.md §5, round 2 finding 12).

A bare store as `client_storage` would hold upstream Clerk tokens and client
secrets in plaintext (`oauth_proxy/proxy.py:584,609,1406`). The Fernet wrapper
stores an envelope dict which the store JSON-serialises, so the raw value IS
readable JSON — these tests read it and assert nothing sensitive is in it.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from smartmemory_mcp.hosted.auth import (
    build_auth,
    build_clerk_provider,
    build_client_storage,
)

from ._hosted_fixtures import hosted_config, memory_store

CLIENTS_COLLECTION = "mcp-oauth-proxy-clients"
ENVELOPE_KEYS = {"__encrypted_data__", "__encryption_version__"}

SECRET_MATERIAL = [
    "mcp-client-id",
    "super-secret-client-secret",
    "https://claude.ai/api/mcp/auth_callback",
    "oat_upstream_clerk_token",
]


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="mcp-client-id",
        client_secret="super-secret-client-secret",
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="A Registered Client",
    )


def test_a_registered_client_is_stored_only_as_a_fernet_envelope() -> None:
    store = memory_store()
    provider = build_clerk_provider(hosted_config(), redis_store=store)

    async def body() -> tuple[dict[str, Any] | None, Any]:
        await provider.register_client(_client())
        raw = await store.get(key="mcp-client-id", collection=CLIENTS_COLLECTION)
        loaded = await provider.get_client("mcp-client-id")
        return raw, loaded

    raw, loaded = anyio.run(body)

    assert raw is not None, "the client should have reached the underlying store"
    assert set(raw) == ENVELOPE_KEYS
    assert raw["__encryption_version__"] == 1

    serialized = json.dumps(raw)
    for secret in SECRET_MATERIAL:
        assert secret not in serialized
    assert "redirect_uris" not in serialized
    assert "client_secret" not in serialized

    # And the wrapper must still round-trip it, or encryption has broken storage.
    assert loaded is not None
    assert loaded.client_id == "mcp-client-id"
    assert loaded.client_name == "A Registered Client"
    assert [str(uri) for uri in loaded.redirect_uris] == [
        "https://claude.ai/api/mcp/auth_callback"
    ]


def test_every_stored_record_in_the_collection_is_an_envelope() -> None:
    """Not just the one key we looked up: nothing may bypass the wrapper."""
    store = memory_store()
    provider = build_clerk_provider(hosted_config(), redis_store=store)

    async def body() -> list[dict[str, Any] | None]:
        await provider.register_client(_client())
        return await store.get_many(
            keys=["mcp-client-id"], collection=CLIENTS_COLLECTION
        )

    for raw in anyio.run(body):
        assert raw is not None
        assert set(raw) == ENVELOPE_KEYS


def test_the_wrapper_chain_round_trips_an_arbitrary_record() -> None:
    store = memory_store()
    storage = build_client_storage(hosted_config(), redis_store=store)
    record = {"access_token": "oat_upstream_clerk_token", "nested": {"a": [1, 2]}}

    async def body():
        await storage.put("k", record, collection="c")
        return await store.get(key="k", collection="c"), await storage.get(
            "k", collection="c"
        )

    raw, decrypted = anyio.run(body)

    assert set(raw) == ENVELOPE_KEYS
    assert "oat_upstream_clerk_token" not in json.dumps(raw)
    assert decrypted == record


def test_a_ttl_less_write_through_the_chain_still_gets_the_default() -> None:
    """The TTL default must survive being wrapped by encryption and size-limit."""
    store = memory_store()
    storage = build_client_storage(hosted_config(), redis_store=store)

    async def body():
        await storage.put("k", {"a": 1}, collection="c")
        return await store.ttl(key="k", collection="c")

    _, remaining = anyio.run(body)

    assert remaining is not None
    assert 29 * 86400 < remaining <= 30 * 86400


def test_build_auth_pins_the_signing_key_to_the_config() -> None:
    """Left unset, FastMCP derives it from the Clerk client secret, which ties
    secret rotation to invalidating every issued token (round 5 should-fix 2)."""
    cfg = hosted_config()
    auth = build_auth(cfg, redis_store=memory_store())

    # FastMCP derives a 32-byte key from whatever string it is given
    # (`oauth_proxy/proxy.py:567-577`), so the assertion is on the derivation,
    # not on the raw string.
    from fastmcp.server.auth.oauth_proxy.proxy import derive_jwt_key

    salt = "fastmcp-jwt-signing-key"
    from_config = derive_jwt_key(low_entropy_material=cfg.jwt_signing_key, salt=salt)
    from_client_secret = derive_jwt_key(
        high_entropy_material=cfg.clerk_oauth_client_secret, salt=salt
    )

    assert auth.server._jwt_signing_key == from_config
    assert auth.server._jwt_signing_key != from_client_secret


def test_build_auth_chain_shape() -> None:
    cfg = hosted_config()
    auth = build_auth(cfg, redis_store=memory_store())

    from smartmemory_mcp.hosted.api_key_verifier import SmartMemoryApiKeyVerifier
    from smartmemory_mcp.hosted.auth import SmartMemoryClerkProvider

    assert isinstance(auth.server, SmartMemoryClerkProvider)
    assert len(auth.verifiers) == 1
    assert isinstance(auth.verifiers[0], SmartMemoryApiKeyVerifier)
    # A global required_scopes would reject an API key AFTER it verified: an API
    # key carries SmartMemory scopes, not OIDC ones (round 1, finding 3).
    assert list(auth.required_scopes or []) == []
