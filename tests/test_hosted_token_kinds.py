"""Which bearer kinds the hosted transport accepts (S4, round 2 finding 13).

Only two credentials get past the transport: a FastMCP reference token minted by
the OAuth proxy, and a SmartMemory API key. A raw Clerk `oat_` token and a
SmartMemory JWT are NOT accepted here — the exchange happens server-side, behind
the MCP server's own token.
"""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
from fastmcp import FastMCP

from smartmemory_mcp.hosted.auth import build_auth
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.middleware import HostedIdentityMiddleware
from smartmemory_mcp.hosted.ratelimit import hosted_asgi_middleware
from smartmemory_mcp.tools import common

from ._hosted_fixtures import (
    API_KEY,
    INITIALIZE_BODY,
    LEGACY_KEY,
    hosted_config,
    mcp_headers,
    memory_store,
)

SM_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEifQ.signature"
RAW_CLERK_TOKEN = "oat_" + "c" * 32
GARBAGE = "not-a-token-at-all"


class _Harness:
    """The hosted auth chain, a hosted identity middleware, and one probe tool."""

    def __init__(self) -> None:
        self.cfg = hosted_config()
        self.svc_api_calls: list[str] = []
        self.cache = ExchangeCache()

        def handler(request: httpx.Request) -> httpx.Response:
            self.svc_api_calls.append(request.url.path)
            if request.url.path == "/auth/me":
                from ._hosted_fixtures import ME_PAYLOAD

                return httpx.Response(200, json=ME_PAYLOAD)
            return httpx.Response(404, json={"error": "unexpected"})

        transport = httpx.MockTransport(handler)
        auth = build_auth(self.cfg, redis_store=memory_store())
        auth.verifiers[0]._transport = transport

        self.mcp = FastMCP(
            "hosted-token-kinds",
            auth=auth,
            middleware=[
                HostedIdentityMiddleware(
                    self.cfg, cache=self.cache, transport=transport
                )
            ],
        )

        @self.mcp.tool
        def probe() -> str:
            backend = common.get_backend()
            return json.dumps(
                {
                    "token": backend._session["access_token"],
                    "team_id": backend._session["team_id"],
                }
            )

        self.app = self.mcp.http_app(
            json_response=True, middleware=hosted_asgi_middleware(self.cfg)
        )

    def run(self, body):
        async def outer():
            async with self.app.router.lifespan_context(self.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="https://mcp.test",
                ) as client:
                    return await body(client)

        return anyio.run(outer)

    async def call_probe(self, client: httpx.AsyncClient, bearer: str):
        response = await client.post(
            "/mcp", headers=mcp_headers(bearer), json=INITIALIZE_BODY
        )
        assert response.status_code == 200, response.text
        session_id = response.headers["mcp-session-id"]
        headers = {**mcp_headers(bearer), "mcp-session-id": session_id}
        await client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        result = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "probe", "arguments": {}},
            },
        )
        assert result.status_code == 200, result.text
        return result.json()["result"]


# --- rejected at the transport ---------------------------------------------------


def test_a_missing_bearer_gets_the_challenge() -> None:
    harness = _Harness()

    async def body(client):
        return await client.post("/mcp", headers=mcp_headers(), json=INITIALIZE_BODY)

    response = harness.run(body)

    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer")
    assert "resource_metadata=" in challenge
    assert harness.svc_api_calls == []


@pytest.mark.parametrize(
    ("label", "bearer"),
    [
        ("garbage", GARBAGE),
        ("a SmartMemory JWT", SM_JWT),
        ("a raw Clerk OAuth token", RAW_CLERK_TOKEN),
        ("an empty string", ""),
    ],
)
def test_unacceptable_bearers_are_refused(label: str, bearer: str) -> None:
    harness = _Harness()

    async def body(client):
        return await client.post(
            "/mcp", headers=mcp_headers(bearer), json=INITIALIZE_BODY
        )

    response = harness.run(body)

    assert response.status_code == 401, f"{label} must not be accepted"
    # None of these look like an API key, so none may cost an /auth/me round trip.
    assert harness.svc_api_calls == []


def test_an_api_key_svc_api_rejects_is_refused() -> None:
    harness = _Harness()
    harness.svc_api_calls.clear()

    def reject(request: httpx.Request) -> httpx.Response:
        harness.svc_api_calls.append(request.url.path)
        return httpx.Response(401, json={"detail": "revoked"})

    harness.mcp.auth.verifiers[0]._transport = httpx.MockTransport(reject)

    async def body(client):
        return await client.post(
            "/mcp", headers=mcp_headers(API_KEY), json=INITIALIZE_BODY
        )

    response = harness.run(body)

    assert response.status_code == 401
    assert harness.svc_api_calls == ["/auth/me"]


# --- accepted ---------------------------------------------------------------------


@pytest.mark.parametrize("key", [API_KEY, LEGACY_KEY])
def test_an_api_key_reaches_the_tool_without_an_exchange(key: str) -> None:
    """Round 5 should-fix 1: a legacy `sk_` key is an API key, and classifying by
    the verifier's claim rather than the prefix is what keeps it off the OAuth
    exchange path."""
    harness = _Harness()

    async def body(client):
        return await harness.call_probe(client, key)

    result = harness.run(body)
    payload = json.loads(result["content"][0]["text"])

    assert result["isError"] is False
    assert payload["token"] == key
    assert payload["team_id"] == "team-personal"
    assert "/auth/clerk/oauth-exchange" not in harness.svc_api_calls
    assert harness.svc_api_calls.count("/auth/me") == 1


def test_the_api_key_verification_is_cached_across_calls() -> None:
    harness = _Harness()

    async def body(client):
        await harness.call_probe(client, API_KEY)
        return await harness.call_probe(client, API_KEY)

    harness.run(body)

    assert harness.svc_api_calls.count("/auth/me") == 1
