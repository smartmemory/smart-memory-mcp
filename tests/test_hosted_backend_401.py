"""svc-api 401 handling on the hosted backend (PLAT-MCP-HOSTED-1 S1).

Covers both override points: `_request` (error-dict path) and `search`
(RuntimeError path) — round 3 must-fix 6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.identity import (
    HostedAuthError,
    VerifiedIdentity,
    build_hosted_backend,
)

FINGERPRINT = "fp-abc"


def _identity() -> VerifiedIdentity:
    return VerifiedIdentity(
        kind="oauth",
        subject="user-1",
        sm_access_token="sm-jwt-abc",
        sm_expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        user_id="user-1",
        tenant_id="tenant-1",
        email="dev@test.com",
        default_team_id="team-personal",
        active_workspace_id="org-42",
        fingerprint=FINGERPRINT,
    )


def _cache_with_entry() -> ExchangeCache:
    cache = ExchangeCache()
    identity = _identity()
    cache.put(identity.fingerprint, identity)
    return cache


def _backend(cache: ExchangeCache) -> HostedRemoteBackend:
    return HostedRemoteBackend(
        api_url="https://api.test",
        api_key="sm-jwt-abc",
        team_id="org-42",
        fingerprint=FINGERPRINT,
        cache=cache,
    )


def _respond(monkeypatch, status: int, body: object = None) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def fake_request(method, url, **kwargs):
        request_kwargs = {
            "headers": kwargs.get("headers"),
            "params": kwargs.get("params"),
        }
        if "json" in kwargs:
            request_kwargs["json"] = kwargs["json"]
        request = httpx.Request(method, url, **request_kwargs)
        seen.append(request)
        return httpx.Response(status, json=body or {}, request=request)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake_request)
    return seen


# --- base behaviour is unchanged ------------------------------------------------


def test_base_backend_request_still_returns_an_error_dict_on_401(monkeypatch) -> None:
    _respond(monkeypatch, 401, {"detail": "nope"})
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True

    result = backend.request("GET", "/memory/mem-1")

    assert isinstance(result, dict)
    assert "401" in result["error"]


def test_base_backend_search_still_raises_runtime_error_on_401(monkeypatch) -> None:
    _respond(monkeypatch, 401, {"detail": "nope"})
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True

    with pytest.raises(RuntimeError):
        backend.search("anything")


# --- hosted overrides -----------------------------------------------------------


def test_request_path_401_invalidates_the_cache_and_raises(monkeypatch) -> None:
    _respond(monkeypatch, 401, {"detail": "nope"})
    cache = _cache_with_entry()
    backend = _backend(cache)

    with pytest.raises(HostedAuthError) as excinfo:
        backend.get("mem-1")

    assert "retry" in str(excinfo.value)
    assert cache.get(FINGERPRINT) is None


def test_search_path_401_invalidates_the_cache_and_raises(monkeypatch) -> None:
    _respond(monkeypatch, 401, {"detail": "nope"})
    cache = _cache_with_entry()
    backend = _backend(cache)

    with pytest.raises(HostedAuthError):
        backend.search("anything")

    assert cache.get(FINGERPRINT) is None


def test_non_401_errors_leave_the_cache_alone(monkeypatch) -> None:
    _respond(monkeypatch, 500, {"detail": "boom"})
    cache = _cache_with_entry()
    backend = _backend(cache)

    result = backend.request("GET", "/memory/mem-1")

    assert isinstance(result, dict) and "500" in result["error"]
    assert cache.get(FINGERPRINT) is not None


def test_successful_calls_are_unaffected(monkeypatch) -> None:
    seen = _respond(monkeypatch, 200, {"ok": True})
    cache = _cache_with_entry()
    backend = _backend(cache)

    assert backend.request("GET", "/memory/mem-1") == {"ok": True}
    assert seen[0].headers["Authorization"] == "Bearer sm-jwt-abc"
    assert seen[0].headers["X-Workspace-Id"] == "org-42"
    assert cache.get(FINGERPRINT) is not None


def test_hosted_search_excludes_speculative_items_server_side(monkeypatch) -> None:
    seen = _respond(monkeypatch, 200, {"results": []})
    backend = _backend(_cache_with_entry())
    backend._session["_bootstrapped"] = True

    backend.search("anything")

    assert json.loads(seen[0].content)["exclude_speculative"] is True


def test_local_search_keeps_its_existing_request_body(monkeypatch) -> None:
    seen = _respond(monkeypatch, 200, {"results": []})
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True

    backend.search("anything")

    assert "exclude_speculative" not in json.loads(seen[0].content)


def test_hosted_direct_get_search_uses_the_query_parameter(monkeypatch) -> None:
    seen = _respond(monkeypatch, 200, {"results": []})
    backend = _backend(_cache_with_entry())
    backend._session["_bootstrapped"] = True

    backend.request("GET", "/memory/search", params={"query": "anything"})

    assert seen[0].url.params["exclude_speculative"] == "true"


# --- strict construction --------------------------------------------------------


def test_build_hosted_backend_defaults_team_to_active_workspace(monkeypatch) -> None:
    monkeypatch.setenv("SMARTMEMORY_API_KEY", "env-key-that-must-not-be-used")
    monkeypatch.setenv("SMARTMEMORY_TEAM_ID", "env-team-that-must-not-be-used")
    cache = _cache_with_entry()

    backend = build_hosted_backend(_identity(), api_url="https://api.test", cache=cache)

    assert isinstance(backend, HostedRemoteBackend)
    assert backend._session["access_token"] == "sm-jwt-abc"
    assert backend._session["team_id"] == "org-42"
    assert backend._session["user_email"] == "dev@test.com"
    assert backend._session["_bootstrapped"] is True


def test_build_hosted_backend_honours_an_explicit_team() -> None:
    backend = build_hosted_backend(
        _identity(),
        team_id="team-other",
        api_url="https://api.test",
        cache=_cache_with_entry(),
    )

    assert backend._session["team_id"] == "team-other"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sm_access_token": ""},
        {"active_workspace_id": None, "default_team_id": None},
    ],
)
def test_build_hosted_backend_refuses_empty_credentials(overrides: dict) -> None:
    from dataclasses import replace

    identity = replace(_identity(), **overrides)

    with pytest.raises(AssertionError):
        build_hosted_backend(
            identity, api_url="https://api.test", cache=ExchangeCache()
        )


def test_build_hosted_backend_refuses_an_empty_api_url() -> None:
    with pytest.raises(AssertionError):
        build_hosted_backend(_identity(), api_url="", cache=ExchangeCache())
