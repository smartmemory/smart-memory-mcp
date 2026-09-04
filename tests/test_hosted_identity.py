"""Per-call hosted identity: dispatch hooks + middleware (PLAT-MCP-HOSTED-1 S3).

The wire-level cases drive a real `tools/call` through `mcp.http_app()` over
`httpx.ASGITransport`, so the middleware, the auth backend and the contextvar
are exercised exactly as they are in production. No network: svc-api is an
`httpx.MockTransport` for the exchange and a patched `httpx.request` for the
backend.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
import httpx
import pytest
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.middleware.middleware import MiddlewareContext

from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.hosted.config import HostedConfig
from smartmemory_mcp.hosted.exchange import ExchangeCache, token_fingerprint
from smartmemory_mcp.hosted.identity import (
    HostedAuthError,
    HostedIdentity,
    VerifiedIdentity,
    current_identity,
    hosted_mode_enabled,
    resolve_hosted_backend,
    set_hosted_mode,
)
from smartmemory_mcp.hosted.middleware import HostedIdentityMiddleware
from smartmemory_mcp.tools import common

API_URL = "https://api.test"
OAUTH_BEARER = "oat_upstream_clerk_token_value"
API_KEY_BEARER = "sm_test_" + "a" * 24
LEGACY_BEARER = "sk_" + "b" * 24

SM_JWT = "sm-jwt-from-exchange"
OAUTH_WORKSPACE = "org-42"
API_KEY_WORKSPACE = "team-personal"


def _config() -> HostedConfig:
    return HostedConfig(
        api_url=API_URL,
        public_base_url="https://mcp.test",
        clerk_domain="clerk.test",
        clerk_oauth_client_id="cid",
        clerk_oauth_client_secret="csecret",
        jwt_signing_key="signing",
        state_encryption_key="fernet",
        redis_url="redis://localhost:6379/0",
    )


def _verified(kind: str = "oauth", **overrides: Any) -> VerifiedIdentity:
    base = {
        "kind": kind,
        "subject": "user-1",
        "sm_access_token": SM_JWT,
        "sm_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "email": "dev@test.com",
        "default_team_id": "team-personal",
        "active_workspace_id": OAUTH_WORKSPACE,
        "fingerprint": token_fingerprint(OAUTH_BEARER),
    }
    base.update(overrides)
    return VerifiedIdentity(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------
# (c)/(d) dispatch hooks
# --------------------------------------------------------------------------------


@pytest.fixture
def hosted_mode():
    set_hosted_mode(True)
    try:
        yield
    finally:
        set_hosted_mode(False)


def test_hosted_mode_defaults_off() -> None:
    assert hosted_mode_enabled() is False


def test_unbound_identity_in_hosted_mode_raises_everywhere(hosted_mode) -> None:
    common.reset_backend()
    dispatch.reset_backend()

    with pytest.raises(HostedAuthError):
        resolve_hosted_backend()
    with pytest.raises(HostedAuthError):
        dispatch.resolve_backend()
    with pytest.raises(HostedAuthError):
        common.get_backend()


def test_bound_identity_is_returned_verbatim_by_both_entry_points(hosted_mode) -> None:
    sentinel = object()
    identity = HostedIdentity(
        verified=_verified(), team_id=OAUTH_WORKSPACE, backend=sentinel
    )
    token = current_identity.set(identity)
    try:
        assert resolve_hosted_backend() is sentinel
        assert dispatch.resolve_backend() is sentinel
        assert common.get_backend() is sentinel
    finally:
        current_identity.reset(token)


def test_local_mode_is_unchanged_when_nothing_is_bound(monkeypatch) -> None:
    common.reset_backend()
    dispatch.reset_backend()
    singleton = object()
    monkeypatch.setattr(common, "resolve_backend", lambda: singleton)

    assert resolve_hosted_backend() is None
    assert common.get_backend() is singleton

    common.reset_backend()


def test_local_mode_still_honours_a_bound_identity(monkeypatch) -> None:
    """Hosted mode off, contextvar set: the per-call backend still wins.

    This is the arm that makes `--hosted` a switch on failure semantics only,
    never on which backend a bound call uses.
    """
    common.reset_backend()
    monkeypatch.setattr(common, "resolve_backend", lambda: object())
    sentinel = object()
    token = current_identity.set(
        HostedIdentity(verified=_verified(), team_id="t", backend=sentinel)
    )
    try:
        assert common.get_backend() is sentinel
    finally:
        current_identity.reset(token)
        common.reset_backend()


# --------------------------------------------------------------------------------
# wire-level harness
# --------------------------------------------------------------------------------


class _StubVerifier(TokenVerifier):
    """Stands in for MultiAuth: classifies by claim exactly as the real chain does."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token in (API_KEY_BEARER, LEGACY_BEARER):
            return AccessToken(
                token=token,
                client_id="api-key",
                scopes=[],
                subject="user-key",
                claims={
                    "kind": "api_key",
                    "tenant_id": "tenant-key",
                    "default_team_id": API_KEY_WORKSPACE,
                    "active_workspace_id": API_KEY_WORKSPACE,
                    "email": "key@test.com",
                },
            )
        if token == OAUTH_BEARER:
            return AccessToken(
                token=token,
                client_id="mcp-client",
                scopes=["openid"],
                subject="clerk-sub",
                claims={"kind": "oauth"},
            )
        return None


def _exchange_payload() -> dict[str, Any]:
    return {
        "access_token": SM_JWT,
        "token_type": "bearer",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "default_team_id": "team-personal",
        "active_workspace_id": OAUTH_WORKSPACE,
        "email": "dev@test.com",
        "org_id": OAUTH_WORKSPACE,
    }


class _Harness:
    """A minimal hosted server plus the request plumbing to drive it."""

    def __init__(self, cache: ExchangeCache | None = None) -> None:
        self.exchange_calls: list[str] = []
        self.cache = cache or ExchangeCache()
        self.started: dict[str, anyio.Event] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            self.exchange_calls.append(request.url.path)
            if request.url.path == "/auth/clerk/oauth-exchange":
                return httpx.Response(200, json=_exchange_payload())
            return httpx.Response(404, json={"error": "unexpected"})

        self.middleware = HostedIdentityMiddleware(
            _config(), cache=self.cache, transport=httpx.MockTransport(handler)
        )
        self.mcp = FastMCP(
            "hosted-test", auth=_StubVerifier(), middleware=[self.middleware]
        )
        self._register_tools()
        self.app = self.mcp.http_app(json_response=True)

    def _register_tools(self) -> None:
        harness = self

        @self.mcp.tool
        def probe() -> str:
            backend = common.get_backend()
            return json.dumps(
                {
                    "token": backend._session["access_token"],
                    "team_id": backend._session["team_id"],
                    "email": backend._session["user_email"],
                    "backend_id": id(backend),
                    "bound": current_identity.get() is not None,
                }
            )

        @self.mcp.tool
        def probe_call() -> str:
            """Reach svc-api through the per-call backend."""
            return json.dumps(common.get_backend().request("GET", "/memory/mem-1"))

        @self.mcp.tool
        async def set_team(team: str, ctx: Context) -> str:
            await ctx.set_state("team_id", team)
            return team

        @self.mcp.tool
        async def probe_concurrent(label: str) -> str:
            backend = common.get_backend()
            harness.started[label].set()
            with anyio.fail_after(10):
                for event in harness.started.values():
                    await event.wait()
            return json.dumps(
                {
                    "token": backend._session["access_token"],
                    "team_id": backend._session["team_id"],
                    "backend_id": id(backend),
                }
            )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://mcp.test"
        )

    @staticmethod
    def _headers(bearer: str, session_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if session_id:
            headers["mcp-session-id"] = session_id
        return headers

    async def open_session(self, client: httpx.AsyncClient, bearer: str) -> str:
        response = await client.post(
            "/mcp",
            headers=self._headers(bearer),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        session_id = response.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            headers=self._headers(bearer, session_id),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        return session_id

    async def call(
        self,
        client: httpx.AsyncClient,
        bearer: str,
        session_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await client.post(
            "/mcp",
            headers=self._headers(bearer, session_id),
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


async def _run(harness: _Harness, body):
    async with harness.app.router.lifespan_context(harness.app):
        async with harness.client() as client:
            return await body(client)


# --------------------------------------------------------------------------------
# (a) both arms reach the tool with the right token and team
# --------------------------------------------------------------------------------


def test_oauth_bearer_reaches_the_tool_as_the_exchanged_identity() -> None:
    harness = _Harness()

    async def body(client):
        session = await harness.open_session(client, OAUTH_BEARER)
        return await harness.call(client, OAUTH_BEARER, session, "probe")

    result = anyio.run(_run, harness, body)
    payload = json.loads(_text(result))

    assert result["isError"] is False
    assert payload["token"] == SM_JWT
    assert payload["team_id"] == OAUTH_WORKSPACE
    assert payload["email"] == "dev@test.com"
    assert payload["bound"] is True
    assert harness.exchange_calls == ["/auth/clerk/oauth-exchange"]


def test_api_key_bearer_reaches_the_tool_as_itself_without_an_exchange() -> None:
    harness = _Harness()

    async def body(client):
        session = await harness.open_session(client, API_KEY_BEARER)
        return await harness.call(client, API_KEY_BEARER, session, "probe")

    result = anyio.run(_run, harness, body)
    payload = json.loads(_text(result))

    assert payload["token"] == API_KEY_BEARER
    assert payload["team_id"] == API_KEY_WORKSPACE
    assert harness.exchange_calls == []


# --------------------------------------------------------------------------------
# (g) a legacy sk_ key is classified by the claim, never by the prefix
# --------------------------------------------------------------------------------


def test_legacy_sk_bearer_is_never_sent_to_the_exchange() -> None:
    harness = _Harness()

    async def body(client):
        session = await harness.open_session(client, LEGACY_BEARER)
        return await harness.call(client, LEGACY_BEARER, session, "probe")

    result = anyio.run(_run, harness, body)
    payload = json.loads(_text(result))

    assert payload["token"] == LEGACY_BEARER
    assert harness.exchange_calls == []


def test_unknown_bearer_is_refused_at_the_transport() -> None:
    harness = _Harness()

    async def body(client):
        return await client.post(
            "/mcp",
            headers=_Harness._headers("garbage"),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

    response = anyio.run(_run, harness, body)

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")


# --------------------------------------------------------------------------------
# (f) the session team is read BEFORE the backend is built
# --------------------------------------------------------------------------------


def test_switching_the_session_team_changes_the_next_call_workspace() -> None:
    harness = _Harness()

    async def body(client):
        session = await harness.open_session(client, OAUTH_BEARER)
        before = await harness.call(client, OAUTH_BEARER, session, "probe")
        await harness.call(
            client, OAUTH_BEARER, session, "set_team", {"team": "team-B"}
        )
        after = await harness.call(client, OAUTH_BEARER, session, "probe")
        return before, after

    before, after = anyio.run(_run, harness, body)

    assert json.loads(_text(before))["team_id"] == OAUTH_WORKSPACE
    assert json.loads(_text(after))["team_id"] == "team-B"
    # The team switch must not force a second exchange — the identity is cached.
    assert harness.exchange_calls == ["/auth/clerk/oauth-exchange"]


def test_a_team_switch_is_scoped_to_its_own_session() -> None:
    harness = _Harness()

    async def body(client):
        first = await harness.open_session(client, OAUTH_BEARER)
        second = await harness.open_session(client, OAUTH_BEARER)
        await harness.call(client, OAUTH_BEARER, first, "set_team", {"team": "team-B"})
        return (
            await harness.call(client, OAUTH_BEARER, first, "probe"),
            await harness.call(client, OAUTH_BEARER, second, "probe"),
        )

    switched, untouched = anyio.run(_run, harness, body)

    assert json.loads(_text(switched))["team_id"] == "team-B"
    assert json.loads(_text(untouched))["team_id"] == OAUTH_WORKSPACE


# --------------------------------------------------------------------------------
# (e) concurrency: two callers in flight never share a backend
# --------------------------------------------------------------------------------


def test_concurrent_calls_with_different_bearers_never_share_a_backend() -> None:
    harness = _Harness()
    harness.started = {"oauth": anyio.Event(), "key": anyio.Event()}
    results: dict[str, dict[str, Any]] = {}

    async def body(client):
        oauth_session = await harness.open_session(client, OAUTH_BEARER)
        key_session = await harness.open_session(client, API_KEY_BEARER)

        async def run(bearer: str, session: str, label: str) -> None:
            result = await harness.call(
                client, bearer, session, "probe_concurrent", {"label": label}
            )
            results[label] = json.loads(_text(result))

        async with anyio.create_task_group() as tg:
            tg.start_soon(run, OAUTH_BEARER, oauth_session, "oauth")
            tg.start_soon(run, API_KEY_BEARER, key_session, "key")

    async def outer(client):
        # anyio.Event must be created inside the running loop.
        harness.started = {"oauth": anyio.Event(), "key": anyio.Event()}
        await body(client)

    anyio.run(_run, harness, outer)

    assert results["oauth"]["token"] == SM_JWT
    assert results["oauth"]["team_id"] == OAUTH_WORKSPACE
    assert results["key"]["token"] == API_KEY_BEARER
    assert results["key"]["team_id"] == API_KEY_WORKSPACE
    assert results["oauth"]["backend_id"] != results["key"]["backend_id"]


# --------------------------------------------------------------------------------
# (h) a svc-api 401 inside the call becomes an Unauthorized tool error
# --------------------------------------------------------------------------------


def test_svc_api_401_inside_a_tool_becomes_an_unauthorized_tool_error(
    monkeypatch,
) -> None:
    harness = _Harness()

    def fake_request(method, url, **kwargs):
        request = httpx.Request(method, url)
        return httpx.Response(401, json={"detail": "nope"}, request=request)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_request)

    async def body(client):
        session = await harness.open_session(client, OAUTH_BEARER)
        return await harness.call(client, OAUTH_BEARER, session, "probe_call")

    result = anyio.run(_run, harness, body)

    assert result["isError"] is True
    assert _text(result).startswith("Unauthorized:")
    assert harness.cache.get(token_fingerprint(OAUTH_BEARER)) is None


def test_a_healthy_call_leaves_the_cached_identity_in_place(monkeypatch) -> None:
    harness = _Harness()

    def fake_request(method, url, **kwargs):
        request = httpx.Request(method, url)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_request)

    async def body(client):
        session = await harness.open_session(client, OAUTH_BEARER)
        return await harness.call(client, OAUTH_BEARER, session, "probe_call")

    result = anyio.run(_run, harness, body)

    assert result["isError"] is False
    assert harness.cache.get(token_fingerprint(OAUTH_BEARER)) is not None


def test_graceful_does_not_swallow_a_hosted_auth_error() -> None:
    """Every hosted tool is wrapped in @graceful, whose bare `except Exception`
    is exactly where an auth failure could be turned into a friendly string and
    lost. It must reach the middleware instead."""

    @common.graceful
    def tool() -> str:
        raise HostedAuthError("session expired, retry")

    with pytest.raises(HostedAuthError):
        tool()


# --------------------------------------------------------------------------------
# (b) the contextvar is always reset — including on the failure paths
# --------------------------------------------------------------------------------


class _FakeContext:
    def __init__(self, team_id: str | None = None) -> None:
        self._team_id = team_id

    async def get_state(self, key: str) -> Any:
        return self._team_id if key == "team_id" else None


def _access_token(kind: str = "api_key", token: str = API_KEY_BEARER) -> AccessToken:
    return AccessToken(
        token=token,
        client_id="api-key",
        scopes=[],
        subject="user-key",
        claims={
            "kind": kind,
            "tenant_id": "tenant-key",
            "default_team_id": API_KEY_WORKSPACE,
            "active_workspace_id": API_KEY_WORKSPACE,
            "email": "key@test.com",
        },
    )


def _middleware_context(team_id: str | None = None) -> MiddlewareContext[Any]:
    return MiddlewareContext(message=object(), fastmcp_context=_FakeContext(team_id))


def _patch_token(monkeypatch, token: AccessToken | None) -> None:
    monkeypatch.setattr(
        "smartmemory_mcp.hosted.middleware.get_access_token", lambda: token
    )


def test_contextvar_is_reset_after_a_successful_call(monkeypatch) -> None:
    _patch_token(monkeypatch, _access_token())
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())
    seen: dict[str, Any] = {}

    async def call_next(context):
        seen["identity"] = current_identity.get()
        return "ok"

    assert anyio.run(middleware.on_call_tool, _middleware_context(), call_next) == "ok"
    assert seen["identity"] is not None
    assert current_identity.get() is None


def test_contextvar_is_reset_after_a_failing_call(monkeypatch) -> None:
    _patch_token(monkeypatch, _access_token())
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())

    async def call_next(context):
        raise ValueError("tool blew up")

    with pytest.raises(ValueError):
        anyio.run(middleware.on_call_tool, _middleware_context(), call_next)

    assert current_identity.get() is None


def test_hosted_auth_error_from_the_tool_becomes_an_unauthorized_tool_error(
    monkeypatch,
) -> None:
    _patch_token(monkeypatch, _access_token())
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())

    async def call_next(context):
        raise HostedAuthError("session expired, retry")

    with pytest.raises(ToolError) as excinfo:
        anyio.run(middleware.on_call_tool, _middleware_context(), call_next)

    assert str(excinfo.value).startswith("Unauthorized:")
    assert current_identity.get() is None


def test_missing_access_token_is_refused(monkeypatch) -> None:
    _patch_token(monkeypatch, None)
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())

    async def call_next(context):  # pragma: no cover - must never run
        raise AssertionError("the tool must not be reached")

    with pytest.raises(ToolError) as excinfo:
        anyio.run(middleware.on_call_tool, _middleware_context(), call_next)

    assert str(excinfo.value).startswith("Unauthorized:")


def test_missing_fastmcp_context_is_refused(monkeypatch) -> None:
    _patch_token(monkeypatch, _access_token())
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())

    async def call_next(context):  # pragma: no cover - must never run
        raise AssertionError("the tool must not be reached")

    context = MiddlewareContext(message=object(), fastmcp_context=None)
    with pytest.raises(ToolError):
        anyio.run(middleware.on_call_tool, context, call_next)

    assert current_identity.get() is None


def test_an_identity_with_no_workspace_is_refused(monkeypatch) -> None:
    token = _access_token()
    token.claims["default_team_id"] = None
    token.claims["active_workspace_id"] = None
    _patch_token(monkeypatch, token)
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())

    async def call_next(context):  # pragma: no cover - must never run
        raise AssertionError("the tool must not be reached")

    with pytest.raises(ToolError) as excinfo:
        anyio.run(middleware.on_call_tool, _middleware_context(), call_next)

    assert str(excinfo.value).startswith("Unauthorized:")


def test_session_team_overrides_the_default_workspace(monkeypatch) -> None:
    _patch_token(monkeypatch, _access_token())
    middleware = HostedIdentityMiddleware(_config(), cache=ExchangeCache())
    seen: dict[str, Any] = {}

    async def call_next(context):
        identity = current_identity.get()
        seen["team_id"] = identity.team_id
        seen["header_team"] = identity.backend._session["team_id"]
        return "ok"

    anyio.run(middleware.on_call_tool, _middleware_context("team-B"), call_next)

    assert seen["team_id"] == "team-B"
    assert seen["header_team"] == "team-B"
