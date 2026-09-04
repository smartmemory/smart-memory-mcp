"""Clerk-token -> SmartMemory-JWT exchange + cache (PLAT-MCP-HOSTED-1 S2)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from smartmemory_mcp.hosted.exchange import (
    ExchangeCache,
    exchange,
    get_sm_identity,
    token_fingerprint,
)
from smartmemory_mcp.hosted.identity import HostedAuthError, VerifiedIdentity

API_URL = "https://api.test"
UPSTREAM = "oat_super_secret_upstream_token"


def _expires(minutes: int = 60) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _payload(expires_at: datetime | None = None) -> dict[str, object]:
    return {
        "access_token": "sm-jwt-abc",
        "token_type": "bearer",
        "expires_at": (expires_at or _expires()).isoformat(),
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "default_team_id": "team-personal",
        "active_workspace_id": "org-42",
        "email": "dev@test.com",
        "org_id": "org-42",
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_token_fingerprint_is_sha256_hex() -> None:
    assert token_fingerprint(UPSTREAM) == hashlib.sha256(UPSTREAM.encode()).hexdigest()


def test_exchange_posts_contract_body_and_maps_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json=_payload())

    with _client(handler) as client:
        identity = exchange(API_URL, UPSTREAM, client=client)

    assert seen["method"] == "POST"
    assert seen["url"] == f"{API_URL}/auth/clerk/oauth-exchange"
    assert seen["body"] == {"token": UPSTREAM}
    assert isinstance(identity, VerifiedIdentity)
    assert identity.kind == "oauth"
    assert identity.sm_access_token == "sm-jwt-abc"
    assert identity.user_id == "user-1"
    assert identity.tenant_id == "tenant-1"
    assert identity.email == "dev@test.com"
    assert identity.default_team_id == "team-personal"
    assert identity.active_workspace_id == "org-42"
    assert identity.fingerprint == token_fingerprint(UPSTREAM)
    assert identity.sm_expires_at.tzinfo is not None


def test_exchange_non_200_raises_hosted_auth_error_with_status_and_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token"})

    with _client(handler) as client, pytest.raises(HostedAuthError) as excinfo:
        exchange(API_URL, UPSTREAM, client=client)

    message = str(excinfo.value)
    assert "401" in message
    assert "invalid_token" in message


def test_exchange_failure_logs_warning_without_the_token(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "account_disabled"})

    with caplog.at_level(logging.WARNING), _client(handler) as client:
        with pytest.raises(HostedAuthError) as excinfo:
            exchange(API_URL, UPSTREAM, client=client)

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "a failed exchange must log at WARNING"
    assert any("403" in r.getMessage() for r in records)
    assert UPSTREAM not in caplog.text
    assert UPSTREAM not in str(excinfo.value)


def test_exchange_transport_error_raises_hosted_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with _client(handler) as client, pytest.raises(HostedAuthError):
        exchange(API_URL, UPSTREAM, client=client)


def _identity(expires_at: datetime, fingerprint: str = "fp") -> VerifiedIdentity:
    return VerifiedIdentity(
        kind="oauth",
        subject="user-1",
        sm_access_token="sm-jwt-abc",
        sm_expires_at=expires_at,
        user_id="user-1",
        tenant_id="tenant-1",
        email="dev@test.com",
        default_team_id="team-personal",
        active_workspace_id="org-42",
        fingerprint=fingerprint,
    )


def test_cache_returns_entry_before_expiry_minus_60s() -> None:
    cache = ExchangeCache()
    identity = _identity(_expires(minutes=5))
    cache.put(identity.fingerprint, identity)

    assert cache.get(identity.fingerprint) is identity


def test_cache_evicts_within_60s_of_expiry() -> None:
    cache = ExchangeCache()
    identity = _identity(datetime.now(timezone.utc) + timedelta(seconds=59))
    cache.put(identity.fingerprint, identity)

    assert cache.get(identity.fingerprint) is None
    assert identity.fingerprint not in cache


def test_cache_invalidate_drops_the_entry() -> None:
    cache = ExchangeCache()
    identity = _identity(_expires())
    cache.put(identity.fingerprint, identity)

    cache.invalidate(identity.fingerprint)

    assert cache.get(identity.fingerprint) is None
    # invalidating an unknown fingerprint is a no-op, never an error
    cache.invalidate("not-present")


def test_cache_is_lru_bounded() -> None:
    cache = ExchangeCache(max_size=3)
    for index in range(4):
        identity = _identity(_expires(), fingerprint=f"fp-{index}")
        cache.put(identity.fingerprint, identity)

    assert len(cache) == 3
    assert cache.get("fp-0") is None
    assert cache.get("fp-3") is not None


def test_get_sm_identity_exchanges_once_then_serves_the_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_payload())

    cache = ExchangeCache()
    with _client(handler) as client:
        first = get_sm_identity(cache, API_URL, UPSTREAM, client=client)
        second = get_sm_identity(cache, API_URL, UPSTREAM, client=client)

    assert len(calls) == 1
    assert second is first
    assert cache.get(token_fingerprint(UPSTREAM)) is first


def test_get_sm_identity_re_exchanges_after_invalidate() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_payload())

    cache = ExchangeCache()
    with _client(handler) as client:
        first = get_sm_identity(cache, API_URL, UPSTREAM, client=client)
        cache.invalidate(first.fingerprint)
        second = get_sm_identity(cache, API_URL, UPSTREAM, client=client)

    assert len(calls) == 2
    assert second is not first
