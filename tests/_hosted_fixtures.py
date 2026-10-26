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


class RoutingSessionStore:
    """svc-api's `routing_sessions` lever as the hosted MCP sees it (WS-ROUTING-4), for MockTransport handlers.

    Rows are keyed by (bearer, client_key): the bearer stands for the caller's (tenant, user), exactly the server's
    key minus the context hint, which hosted MCP never sends. It survives any number of MCP sessions and middleware
    instances, as the Mongo record does. `forbidden` workspaces answer 403 (not a member); `bound` bearers answer 409.
    """

    def __init__(
        self, *, forbidden: tuple[str, ...] = (), bound: tuple[str, ...] = ()
    ) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        self.forbidden, self.bound = set(forbidden), set(bound)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    @staticmethod
    def _bearer(request: httpx.Request) -> str:
        return request.headers.get("Authorization", "").removeprefix("Bearer ")

    def handle(self, request: httpx.Request) -> httpx.Response | None:
        """The routing response for `request`, or None when the path is not a routing route."""
        import json as _json

        path = request.url.path
        if path == "/memory/routing/session" and request.method == "PUT":
            body = _json.loads(request.content)
            self.calls.append(
                ("PUT", path, {"body": body, "headers": dict(request.headers)})
            )
            bearer = self._bearer(request)
            if bearer in self.bound:
                return httpx.Response(
                    409, json={"detail": {"error": "client_bound_to_workspace"}}
                )
            if body["workspace_id"] in self.forbidden:
                return httpx.Response(
                    403, json={"detail": "insufficient_team_permissions"}
                )
            self.rows[(bearer, body["client_key"])] = body["workspace_id"]
            return httpx.Response(
                200,
                json={"workspace_id": body["workspace_id"], "set_via": "mcp_switch"},
            )
        if path == "/memory/routing/explain" and request.method == "GET":
            client_key = request.url.params.get("client_key", "")
            self.calls.append(
                (
                    "GET",
                    path,
                    {"client_key": client_key, "headers": dict(request.headers)},
                )
            )
            chosen = self.rows.get((self._bearer(request), client_key))
            candidate = {
                "lever": "session",
                "workspace_id": chosen,
                "status": "chosen" if chosen else "absent",
            }
            return httpx.Response(
                200,
                json={
                    "workspace_id": chosen,
                    "routed_by": "session" if chosen else None,
                    "candidates": [
                        {"lever": "call", "workspace_id": None, "status": "absent"},
                        candidate,
                    ],
                },
            )
        return None

    def transport(self, fallback=None) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            routed = self.handle(request)
            if routed is not None:
                return routed
            if fallback is not None:
                return fallback(request)
            return httpx.Response(404, json={"detail": "unexpected"})

        return httpx.MockTransport(handler)
