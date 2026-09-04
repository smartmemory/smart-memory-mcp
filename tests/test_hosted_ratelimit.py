"""Rate limits on the hosted public surface (S4, design.md §5).

Driven against the same ASGI object `run_hosted` hands to uvicorn (round 2
finding 2 / round 4 note 1), so the limits cannot be attached to one app and
tested on another.
"""

from __future__ import annotations

import logging

import anyio
import httpx
import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from smartmemory_mcp.hosted.auth import build_auth
from smartmemory_mcp.hosted.ratelimit import (
    HostedRateLimit,
    OAUTH_PER_IP_PER_MINUTE,
    REGISTER_GLOBAL_PER_HOUR,
    REGISTER_PER_IP_PER_MINUTE,
    UNAUTHENTICATED_MCP_PER_IP_PER_MINUTE,
    hosted_asgi_middleware,
)

from ._hosted_fixtures import API_KEY, hosted_config, memory_store


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _bare_app(cfg=None, clock=None) -> Starlette:
    """A stand-in app so the limiter is tested without FastMCP's own behaviour."""

    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/register", ok, methods=["GET", "POST"]),
            Route("/authorize", ok),
            Route("/token", ok, methods=["POST"]),
            Route("/mcp", ok, methods=["POST"]),
            Route("/health", ok),
        ]
    )
    app.add_middleware(
        HostedRateLimit, config=cfg or hosted_config(), clock=clock or _Clock()
    )
    return app


def _run(app, body):
    async def outer():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 1234)),
            base_url="https://mcp.test",
        ) as client:
            return await body(client)

    return anyio.run(outer)


def test_the_descriptor_is_what_gets_exported() -> None:
    """BaseHTTPMiddleware needs `app` first, so an instance cannot be passed to
    FastMCP — only the descriptor (round 3, must-fix 2)."""
    middleware = hosted_asgi_middleware(hosted_config())

    assert len(middleware) == 1
    assert middleware[0].cls is HostedRateLimit
    assert middleware[0].kwargs["config"].trust_proxy is False


def test_register_is_capped_per_ip() -> None:
    app = _bare_app()

    async def body(client):
        statuses = []
        for _ in range(REGISTER_PER_IP_PER_MINUTE + 1):
            statuses.append(await client.post("/register", json={}))
        return statuses

    responses = _run(app, body)

    assert [r.status_code for r in responses[:-1]] == [200] * REGISTER_PER_IP_PER_MINUTE
    assert responses[-1].status_code == 429
    assert int(responses[-1].headers["Retry-After"]) > 0
    assert responses[-1].json()["error"] == "rate_limited"


def test_the_register_window_reopens() -> None:
    clock = _Clock()
    app = _bare_app(clock=clock)

    async def body(client):
        for _ in range(REGISTER_PER_IP_PER_MINUTE):
            await client.post("/register", json={})
        blocked = await client.post("/register", json={})
        clock.advance(61)
        return blocked, await client.post("/register", json={})

    blocked, after = _run(app, body)

    assert blocked.status_code == 429
    assert after.status_code == 200


def test_the_global_register_budget_is_shared_across_ips_and_warns(caplog) -> None:
    """The per-IP cap alone is useless against a flood from many addresses; the
    global ceiling is what protects the client store."""
    clock = _Clock()
    app = _bare_app(clock=clock)

    async def body(client):
        statuses = []
        for index in range(REGISTER_GLOBAL_PER_HOUR + 1):
            # A fresh minute for every 10 requests, so the per-IP cap never fires.
            if index % REGISTER_PER_IP_PER_MINUTE == 0:
                clock.advance(61)
            statuses.append((await client.post("/register", json={})).status_code)
        return statuses

    with caplog.at_level(logging.WARNING):
        statuses = _run(app, body)

    assert statuses[-1] == 429
    assert statuses.count(200) == REGISTER_GLOBAL_PER_HOUR
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "exhausting the global budget must log at WARNING"
    assert any("203.0.113.7" in r.getMessage() for r in warnings)


@pytest.mark.parametrize(
    ("path", "method"), [("/authorize", "GET"), ("/token", "POST")]
)
def test_the_oauth_endpoints_are_capped_per_ip(path: str, method: str) -> None:
    app = _bare_app()

    async def body(client):
        statuses = []
        for _ in range(OAUTH_PER_IP_PER_MINUTE + 1):
            statuses.append((await client.request(method, path)).status_code)
        return statuses

    statuses = _run(app, body)

    assert statuses.count(200) == OAUTH_PER_IP_PER_MINUTE
    assert statuses[-1] == 429


def test_authorize_and_token_have_separate_budgets() -> None:
    app = _bare_app()

    async def body(client):
        for _ in range(OAUTH_PER_IP_PER_MINUTE):
            await client.get("/authorize")
        return await client.post("/token")

    assert _run(app, body).status_code == 200


def test_unauthenticated_mcp_is_capped_but_authenticated_mcp_is_not() -> None:
    app = _bare_app()

    async def body(client):
        anon = []
        for _ in range(UNAUTHENTICATED_MCP_PER_IP_PER_MINUTE + 1):
            anon.append((await client.post("/mcp", json={})).status_code)
        authed = await client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {API_KEY}"}
        )
        return anon, authed

    anon, authed = _run(app, body)

    assert anon.count(200) == UNAUTHENTICATED_MCP_PER_IP_PER_MINUTE
    assert anon[-1] == 429
    # An authenticated caller is accounted for by svc-api, not here.
    assert authed.status_code == 200


def test_unbudgeted_paths_are_untouched() -> None:
    app = _bare_app()

    async def body(client):
        return [(await client.get("/health")).status_code for _ in range(200)]

    assert set(_run(app, body)) == {200}


# --- client identification --------------------------------------------------------


def _ip_seen(cfg, headers: dict[str, str]) -> str:
    """The address the limiter would key a bucket on for this request."""
    seen: list[str] = []

    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/register", ok, methods=["POST"])])
    app.add_middleware(HostedRateLimit, config=cfg, clock=_Clock())

    original = HostedRateLimit.client_ip

    def spy(self, request):
        result = original(self, request)
        seen.append(result)
        return result

    async def body(client):
        return await client.post("/register", json={}, headers=headers)

    HostedRateLimit.client_ip = spy
    try:
        _run(app, body)
    finally:
        HostedRateLimit.client_ip = original

    return seen[0]


def test_forwarded_for_is_ignored_unless_the_proxy_is_trusted() -> None:
    """Honouring X-Forwarded-For unconditionally would let any caller mint a
    fresh bucket per request by varying the header."""
    untrusted = _ip_seen(
        hosted_config(), {"X-Forwarded-For": "198.51.100.9, 203.0.113.1"}
    )
    trusted = _ip_seen(
        hosted_config(trust_proxy=True),
        {"X-Forwarded-For": "198.51.100.9, 203.0.113.1"},
    )

    assert untrusted == "203.0.113.7"  # the real peer
    assert trusted == "198.51.100.9"  # the client Caddy reports


def test_a_trusted_proxy_with_no_header_falls_back_to_the_peer() -> None:
    assert _ip_seen(hosted_config(trust_proxy=True), {}) == "203.0.113.7"


# --- the real hosted app ----------------------------------------------------------


def test_the_limits_are_attached_to_the_served_fastmcp_app() -> None:
    """The limiter must be on the object uvicorn serves, not on a sibling."""
    cfg = hosted_config()
    auth = build_auth(cfg, redis_store=memory_store())
    mcp = FastMCP("hosted-ratelimit-test", auth=auth)

    @mcp.tool
    def probe() -> str:
        return "ok"

    app = mcp.http_app(json_response=True, middleware=hosted_asgi_middleware(cfg))

    async def body(client):
        statuses = []
        for _ in range(REGISTER_PER_IP_PER_MINUTE + 1):
            response = await client.post(
                "/register",
                json={"redirect_uris": ["http://localhost:1234/cb"]},
            )
            statuses.append(response.status_code)
        return statuses

    async def outer():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 1234)),
                base_url="https://mcp.test",
            ) as client:
                return await body(client)

    statuses = anyio.run(outer)

    assert statuses[-1] == 429, statuses
    assert 429 not in statuses[:REGISTER_PER_IP_PER_MINUTE]
