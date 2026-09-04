"""SmartMemory API-key TokenVerifier (PLAT-MCP-HOSTED-1 S2)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from smartmemory_mcp.hosted.api_key_verifier import SmartMemoryApiKeyVerifier

API_URL = "https://api.test"
LIVE_KEY = "sm_live_" + "a" * 24
TEST_KEY = "sm_test_" + "b" * 24
LEGACY_KEY = "sk_" + "c" * 24

ME_PAYLOAD = {
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


def _verifier(handler, calls: list[httpx.Request] | None = None):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return handler(request)

    return SmartMemoryApiKeyVerifier(
        api_url=API_URL, transport=httpx.MockTransport(wrapped)
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=ME_PAYLOAD)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-key",
        "oat_" + "d" * 24,
        "eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "sm_live_short",
        "sm_dev_" + "e" * 24,
        "SM_LIVE_" + "f" * 24,
    ],
)
def test_prefix_mismatch_returns_none_without_a_network_call(token: str) -> None:
    calls: list[httpx.Request] = []
    verifier = _verifier(_ok, calls)

    assert asyncio.run(verifier.verify_token(token)) is None
    assert calls == []


@pytest.mark.parametrize("key", [LIVE_KEY, TEST_KEY, LEGACY_KEY])
def test_valid_key_returns_access_token_with_claims(key: str) -> None:
    calls: list[httpx.Request] = []
    verifier = _verifier(_ok, calls)

    token = asyncio.run(verifier.verify_token(key))

    assert token is not None
    assert token.token == key
    assert token.client_id == "api-key"
    assert token.scopes == []
    assert token.claims["sub"] == "user-1"
    assert token.expires_at is None
    assert token.claims["kind"] == "api_key"
    assert token.claims["tenant_id"] == "tenant-1"
    assert token.claims["default_team_id"] == "team-personal"
    assert token.claims["active_workspace_id"] == "team-personal"
    assert token.claims["email"] == "dev@test.com"

    assert len(calls) == 1
    assert str(calls[0].url) == f"{API_URL}/auth/me"
    assert calls[0].headers["Authorization"] == f"Bearer {key}"


def test_rejected_key_returns_none_and_logs_warning_without_the_key(caplog) -> None:
    calls: list[httpx.Request] = []
    verifier = _verifier(
        lambda request: httpx.Response(401, json={"detail": "nope"}), calls
    )

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(verifier.verify_token(LIVE_KEY)) is None

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a rejected key must log at WARNING"
    assert any("401" in r.getMessage() for r in warnings)
    assert LIVE_KEY not in caplog.text
    assert len(calls) == 1


def test_transport_failure_returns_none_and_logs_warning(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    verifier = _verifier(handler)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(verifier.verify_token(LIVE_KEY)) is None

    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert LIVE_KEY not in caplog.text


def test_positive_result_is_cached_so_the_second_call_skips_the_network() -> None:
    calls: list[httpx.Request] = []
    verifier = _verifier(_ok, calls)

    first = asyncio.run(verifier.verify_token(LIVE_KEY))
    second = asyncio.run(verifier.verify_token(LIVE_KEY))

    assert first is not None and second is not None
    assert second.claims["sub"] == first.claims["sub"]
    assert len(calls) == 1


def test_cache_expires_after_the_ttl() -> None:
    calls: list[httpx.Request] = []
    verifier = _verifier(_ok, calls)
    clock = {"now": 1000.0}
    verifier._clock = lambda: clock["now"]

    asyncio.run(verifier.verify_token(LIVE_KEY))
    clock["now"] += 61.0
    asyncio.run(verifier.verify_token(LIVE_KEY))

    assert len(calls) == 2


def test_failures_are_not_cached() -> None:
    calls: list[httpx.Request] = []
    verifier = _verifier(lambda request: httpx.Response(401), calls)

    asyncio.run(verifier.verify_token(LIVE_KEY))
    asyncio.run(verifier.verify_token(LIVE_KEY))

    assert len(calls) == 2
