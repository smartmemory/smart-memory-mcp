"""Shared harness for the hosted auth tests (PLAT-MCP-HOSTED-1 S4)."""

from __future__ import annotations

from typing import Any

import httpx
from cryptography.fernet import Fernet
from key_value.aio.stores.memory import MemoryStore

from smartmemory_mcp.hosted.config import HostedConfig

API_URL = "https://api.test"
PUBLIC_BASE_URL = "https://mcp.test"
CLERK_DOMAIN = "clerk.test"
CLIENT_ID = "clerk-client-id"
SIGNING_KEY = "a-long-enough-hosted-signing-key"

API_KEY = "sm_test_" + "a" * 24
LEGACY_KEY = "sk_" + "b" * 24

ME_PAYLOAD: dict[str, Any] = {
    "id": "user-1",
    "email": "dev@test.com",
    "full_name": "Dev Test",
    "tenant_id": "tenant-1",
    "subscription_tier": "pro",
    "is_active": True,
    "is_verified": True,
    "roles": ["user"],
    "default_team_id": "team-personal",
}


# The environment a hosted process requires, as `main()` would read it.
REQUIRED_HOSTED_ENV: dict[str, str] = {
    "SMARTMEMORY_API_URL": API_URL,
    "MCP_PUBLIC_BASE_URL": PUBLIC_BASE_URL,
    "CLERK_DOMAIN": CLERK_DOMAIN,
    "CLERK_OAUTH_CLIENT_ID": CLIENT_ID,
    "CLERK_OAUTH_CLIENT_SECRET": "clerk-client-secret",
    "MCP_JWT_SIGNING_KEY": SIGNING_KEY,
    "MCP_STATE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "MCP_REDIS_URL": "redis://localhost:6379/0",
}


def hosted_config(**overrides: Any) -> HostedConfig:
    values: dict[str, Any] = {
        "api_url": API_URL,
        "public_base_url": PUBLIC_BASE_URL,
        "clerk_domain": CLERK_DOMAIN,
        "clerk_oauth_client_id": CLIENT_ID,
        "clerk_oauth_client_secret": "clerk-client-secret",
        "jwt_signing_key": SIGNING_KEY,
        "state_encryption_key": Fernet.generate_key().decode(),
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(overrides)
    return HostedConfig(**values)


def memory_store() -> MemoryStore:
    """Stands in for RedisStore so no test needs a Redis."""
    return MemoryStore()


def auth_me_transport(
    calls: list[httpx.Request] | None = None, status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if request.url.path == "/auth/me" and status == 200:
            return httpx.Response(200, json=ME_PAYLOAD)
        return httpx.Response(status, json={"detail": "rejected"})

    return httpx.MockTransport(handler)


def mcp_headers(bearer: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
