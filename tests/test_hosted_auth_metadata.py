"""Hosted OAuth discovery metadata and the transport auth envelope (S4).

Drives the real ASGI app FastMCP builds, with the rate-limit middleware attached
exactly as `run_hosted` will attach it.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest
from fastmcp import FastMCP

from smartmemory_mcp.hosted.auth import build_auth
from smartmemory_mcp.hosted.ratelimit import hosted_asgi_middleware

from ._hosted_fixtures import (
    API_KEY,
    INITIALIZE_BODY,
    auth_me_transport,
    hosted_config,
    mcp_headers,
    memory_store,
)


def _app(cfg=None, calls: list[httpx.Request] | None = None):
    cfg = cfg or hosted_config()
    auth = build_auth(cfg, redis_store=memory_store())
    # Point the API-key verifier at the mocked svc-api.
    auth.verifiers[0]._transport = auth_me_transport(calls)
    mcp = FastMCP("hosted-metadata-test", auth=auth)

    @mcp.tool
    def probe() -> str:
        return "ok"

    return mcp.http_app(json_response=True, middleware=hosted_asgi_middleware(cfg))


def _run(app, body):
    async def outer():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
            ) as client:
                return await body(client)

    return anyio.run(outer)


def test_authorization_server_metadata_advertises_pkce_and_registration() -> None:
    app = _app()

    async def body(client):
        return await client.get("/.well-known/oauth-authorization-server")

    response = _run(app, body)

    assert response.status_code == 200
    metadata: dict[str, Any] = response.json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["registration_endpoint"]
    # CIMD is what lets Claude Code and Grok connect without pre-registration.
    assert metadata["client_id_metadata_document_supported"] is True
    assert "authorization_endpoint" in metadata
    assert "token_endpoint" in metadata


def test_protected_resource_metadata_is_served_where_the_challenge_points() -> None:
    """RFC 9728 metadata is mounted per resource path, so it lives at
    `/.well-known/oauth-protected-resource/mcp`, not at the bare well-known
    path. The challenge header is the authority on where; this asserts the two
    agree, which is what a client actually follows."""
    app = _app()

    async def body(client):
        challenge = (
            await client.post("/mcp", headers=mcp_headers(), json=INITIALIZE_BODY)
        ).headers["www-authenticate"]
        url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        return url, await client.get(url)

    url, response = _run(app, body)

    assert url == "https://mcp.test/.well-known/oauth-protected-resource/mcp"
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"]
    assert metadata["authorization_servers"]


def test_advertised_scopes_include_the_org_scope() -> None:
    app = _app()

    async def body(client):
        return await client.get("/.well-known/oauth-authorization-server")

    metadata = _run(app, body).json()

    assert "user:org:read" in metadata["scopes_supported"]
    assert "offline_access" in metadata["scopes_supported"]


def test_unauthenticated_mcp_post_is_a_401_challenge() -> None:
    """Transport-level failure: an HTTP challenge, never a tool error
    (design.md §5, round 2 finding 13)."""
    app = _app()

    async def body(client):
        return await client.post("/mcp", headers=mcp_headers(), json=INITIALIZE_BODY)

    response = _run(app, body)

    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer")
    assert "resource_metadata=" in challenge


def test_a_valid_api_key_initializes_successfully() -> None:
    calls: list[httpx.Request] = []
    app = _app(calls=calls)

    async def body(client):
        return await client.post(
            "/mcp", headers=mcp_headers(API_KEY), json=INITIALIZE_BODY
        )

    response = _run(app, body)

    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "hosted-metadata-test"
    assert [request.url.path for request in calls] == ["/auth/me"]


@pytest.mark.parametrize("path", ["/.well-known/oauth-authorization-server"])
def test_discovery_needs_no_bearer(path: str) -> None:
    app = _app()

    async def body(client):
        return await client.get(path)

    assert _run(app, body).status_code == 200
