"""The hosted server's first tool result in a session carries one CLI hint (MCP-FIRST-RUN-HINT-1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import MiddlewareContext
from mcp.types import TextContent

from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.middleware import FIRST_RUN_HINT, HostedIdentityMiddleware
from tests.test_hosted_identity import (
    API_KEY_BEARER,
    OAUTH_BEARER,
    _access_token,
    _config,
    _Harness,
    _patch_token,
    _run,
)

FORBIDDEN = ("local mode", "--local")


class _Ctx:
    """A request context with an MCP session id (the hosted transport is stateful)."""

    def __init__(self, session_id: str | None) -> None:
        self.session_id = session_id

    async def get_state(self, key: str) -> Any:
        return None


def _call(middleware, session_id, call_next):
    context = MiddlewareContext(message=object(), fastmcp_context=_Ctx(session_id))
    return anyio.run(middleware.on_call_tool, context, call_next)


def _texts(result) -> list[str]:
    return [block.text for block in result.content]


async def _ok(context):
    return SimpleNamespace(content=[TextContent(type="text", text="result")])


async def _boom(context):
    raise ValueError("tool blew up")


def _middleware(monkeypatch) -> HostedIdentityMiddleware:
    _patch_token(monkeypatch, _access_token())
    return HostedIdentityMiddleware(_config(), cache=ExchangeCache())


def first_then_never_again(monkeypatch):
    m = _middleware(monkeypatch)
    assert _texts(_call(m, "s1", _ok)) == ["result", FIRST_RUN_HINT]
    assert _texts(_call(m, "s1", _ok)) == ["result"]
    assert _texts(_call(m, "s1", _ok)) == ["result"]


def each_session_gets_its_own(monkeypatch):
    m = _middleware(monkeypatch)
    assert _texts(_call(m, "s1", _ok)) == ["result", FIRST_RUN_HINT]
    assert _texts(_call(m, "s2", _ok)) == ["result", FIRST_RUN_HINT]
    assert _texts(_call(m, "s1", _ok)) == ["result"]


def failed_first_call_keeps_the_hint_for_the_next(monkeypatch):
    m = _middleware(monkeypatch)
    with pytest.raises(ValueError):
        _call(m, "s1", _boom)
    assert _texts(_call(m, "s1", _ok)) == ["result", FIRST_RUN_HINT]


def no_session_id_no_hint(monkeypatch):
    m = _middleware(monkeypatch)
    assert _texts(_call(m, None, _ok)) == ["result"]


def oldest_session_evicted_not_unbounded(monkeypatch):
    monkeypatch.setattr("smartmemory_mcp.hosted.middleware.HINTED_SESSIONS_MAX", 2)
    m = _middleware(monkeypatch)
    for sid in ("a", "b", "c"):
        _call(m, sid, _ok)
    assert len(m._hinted_sessions) == 2 and "a" not in m._hinted_sessions


def unauthenticated_call_says_how_to_sign_in(monkeypatch):
    _patch_token(monkeypatch, None)
    m = HostedIdentityMiddleware(_config(), cache=ExchangeCache())
    with pytest.raises(ToolError) as excinfo:
        _call(m, "s1", _ok)
    message = str(excinfo.value)
    assert message.startswith("Unauthorized: no access token")
    assert "OAuth" in message and "API key" in message and "https://www.smartmemory.ai" in message
    assert not any(bad in message for bad in FORBIDDEN)


def lost_authorization_says_how_to_sign_in(monkeypatch):
    from smartmemory_mcp.hosted.identity import HostedAuthError

    m = _middleware(monkeypatch)

    async def expired(context):
        raise HostedAuthError("session expired, retry")

    with pytest.raises(ToolError) as excinfo:
        _call(m, "s1", expired)
    assert "session expired, retry" in str(excinfo.value) and "API key" in str(excinfo.value)


def over_the_wire_once_per_session_and_never_local_mode(monkeypatch):
    harness = _Harness()

    async def body(client):
        results = []
        for bearer in (API_KEY_BEARER, OAUTH_BEARER):
            session = await harness.open_session(client, bearer)
            for _ in range(2):
                results.append(await harness.call(client, bearer, session, "probe"))
        return results

    results = anyio.run(_run, harness, body)
    shown = [[b["text"] for b in r["content"]].count(FIRST_RUN_HINT) for r in results]
    assert shown == [1, 0, 1, 0]
    assert all(r["isError"] is False for r in results)
    assert not [b["text"] for r in results for b in r["content"] if any(x in b["text"] for x in FORBIDDEN)]


ROWS = [
    first_then_never_again,
    each_session_gets_its_own,
    failed_first_call_keeps_the_hint_for_the_next,
    no_session_id_no_hint,
    oldest_session_evicted_not_unbounded,
    unauthenticated_call_says_how_to_sign_in,
    lost_authorization_says_how_to_sign_in,
    over_the_wire_once_per_session_and_never_local_mode,
]


@pytest.mark.parametrize("scenario", ROWS, ids=[r.__name__ for r in ROWS])
def test_first_run_hint(scenario, monkeypatch):
    scenario(monkeypatch)
