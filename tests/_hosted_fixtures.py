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


# --- Route policy (WS-ROUTING-5 fix round 1, review M1) --------------------------------------------------------------
# Every svc-api `/memory/*` route the MCP calls, with its real scope policy, copied from smart-memory-service origin/main
# `26697be` (`memory_service/api/routes/<file>:<line>` of the route, then its router's `create_router(scope_policy=...)`;
# `base_router.py:62` defaults to team_required). Common origin/main `6f46556` runs the write-routing resolver only for
# team_required memory routes (`service_common/auth/scope.py:530-533`). A header-less call on a user_only route reaches
# `get_scope_provider`, which reads explicit headers only (`auth/core.py:260-274`) and refuses 400 missing_team_context
# (`auth/core.py:299-315`): the session pick never applies there and never refreshes.
TEAM_REQUIRED, USER_ONLY, TENANT_REQUIRED = (
    "team_required",
    "user_only",
    "tenant_required",
)
MCP_ROUTE_POLICY: tuple[tuple[str, str, str, str], ...] = (
    ("POST", "/memory/add", USER_ONLY, "crud.py:498, router crud.py:258"),
    ("POST", "/memory/search", USER_ONLY, "crud.py:1759, router crud.py:258"),
    ("GET", "/memory/list", USER_ONLY, "crud.py:970, router crud.py:258"),
    ("GET", "/memory/by-metadata", USER_ONLY, "crud.py:762, router crud.py:258"),
    ("DELETE", "/memory/clear-all", USER_ONLY, "crud.py:1435, router crud.py:258"),
    ("GET", "/memory/{id}", USER_ONLY, "crud.py:1152, router crud.py:258"),
    ("PATCH", "/memory/{id}", USER_ONLY, "crud.py:1343, router crud.py:258"),
    ("DELETE", "/memory/{id}", USER_ONLY, "crud.py:1580, router crud.py:258"),
    (
        "GET",
        "/memory/{id}/explain",
        TEAM_REQUIRED,
        "explain.py:28, router explain.py:24",
    ),
    (
        "POST",
        "/memory/ingest",
        TEAM_REQUIRED,
        "ingest.py:102, router ingest.py:98 (default)",
    ),
    (
        "POST",
        "/memory/ingest/conversation",
        TEAM_REQUIRED,
        "ingest.py:755, router ingest.py:98 (default)",
    ),
    (
        "POST",
        "/memory/ingest/document",
        TEAM_REQUIRED,
        "structured.py:93, router structured.py:26",
    ),
    (
        "POST",
        "/memory/read-around",
        TEAM_REQUIRED,
        "read_around.py:73, router read_around.py:35",
    ),
    (
        "POST",
        "/memory/recall/pack",
        USER_ONLY,
        "recall_pack.py:36, router recall_pack.py:28",
    ),
    (
        "POST",
        "/memory/result-feedback",
        USER_ONLY,
        "result_feedback.py:84, router result_feedback.py:33",
    ),
    ("GET", "/memory/policy/bundle", USER_ONLY, "policy.py:42, router policy.py:38"),
    ("GET", "/memory/health", USER_ONLY, "insights.py:160, router insights.py:33"),
    ("GET", "/memory/decisions", USER_ONLY, "decisions.py:277, router decisions.py:30"),
    (
        "GET",
        "/memory/decisions/search",
        USER_ONLY,
        "decisions.py:357, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/create",
        USER_ONLY,
        "decisions.py:185, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/pending/create",
        USER_ONLY,
        "decisions.py:415, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/pending/{id}/resolve",
        USER_ONLY,
        "decisions.py:448, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/pending/{id}/activate",
        USER_ONLY,
        "decisions.py:479, router decisions.py:30",
    ),
    (
        "GET",
        "/memory/decisions/{id}",
        USER_ONLY,
        "decisions.py:500, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/{id}/supersede",
        USER_ONLY,
        "decisions.py:528, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/{id}/retract",
        USER_ONLY,
        "decisions.py:577, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/{id}/reinforce",
        USER_ONLY,
        "decisions.py:606, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/{id}/contradict",
        USER_ONLY,
        "decisions.py:636, router decisions.py:30",
    ),
    (
        "GET",
        "/memory/decisions/{id}/provenance",
        USER_ONLY,
        "decisions.py:666, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/decisions/{id}/conflicts",
        USER_ONLY,
        "decisions.py:737, router decisions.py:30",
    ),
    (
        "POST",
        "/memory/code/index",
        TEAM_REQUIRED,
        "code.py:322 add_api_route, router code.py:45",
    ),
    ("GET", "/memory/code/search", TEAM_REQUIRED, "code.py:330, router code.py:45"),
    (
        "GET",
        "/memory/code/dependencies",
        TEAM_REQUIRED,
        "code.py:599, router code.py:45",
    ),
    ("GET", "/memory/code/dead-code", TEAM_REQUIRED, "code.py:740, router code.py:45"),
    (
        "POST",
        "/memory/graph/resolve-aliases",
        TEAM_REQUIRED,
        "graph_maintenance.py:28, router graph_maintenance.py:25 (default)",
    ),
    (
        "GET",
        "/memory/okf/export",
        USER_ONLY,
        "portability.py:532, router portability.py:53",
    ),
    (
        "POST",
        "/memory/okf/import",
        USER_ONLY,
        "portability.py:641, router portability.py:53",
    ),
    (
        "GET",
        "/memory/agents/{id}/recall-profile",
        TEAM_REQUIRED,
        "agents.py:284, router agents.py:46",
    ),
    (
        "PUT",
        "/memory/agents/{id}/recall-profile",
        TEAM_REQUIRED,
        "agents.py:302, router agents.py:46",
    ),
    # Not data routes: never resolved (the listing is tenant-scoped; routing routes are answered by `handle`).
    (
        "GET",
        "/memory/workspaces",
        TENANT_REQUIRED,
        "workspaces.py:364, router workspaces.py:59",
    ),
    (
        "GET",
        "/memory/routing/explain",
        USER_ONLY,
        "routing.py:232, router routing.py:46",
    ),
    (
        "PUT",
        "/memory/routing/session",
        USER_ONLY,
        "routing.py:163, router routing.py:46",
    ),
)
DATA_POLICIES = frozenset({TEAM_REQUIRED, USER_ONLY})


def route_policy(method: str, path: str) -> str:
    """The service policy of an MCP-called `/memory/*` route; an unlisted route fails the test (add it to the table)."""
    parts = path.rstrip("/").split("/")
    for m, pattern, policy, _where in MCP_ROUTE_POLICY:
        pat = pattern.split("/")
        if m == method.upper() and len(pat) == len(parts):
            if all(p == "{id}" or p == q for p, q in zip(pat, parts)):
                # A literal sibling (`/memory/list`) wins over `/memory/{id}`: rows are tried literal-first below.
                if "{id}" in pattern and any(
                    m2 == m and p2 == path.rstrip("/")
                    for m2, p2, _, _ in MCP_ROUTE_POLICY
                ):
                    continue
                return policy
    raise AssertionError(
        f"{method} {path} is not in MCP_ROUTE_POLICY: add the route with its real service policy (file:line)"
    )


class RoutingSessionStore:
    """svc-api's `routing_sessions` lever as the hosted MCP sees it (WS-ROUTING-4), for MockTransport handlers.

    Rows are keyed by (bearer, client_key): the bearer stands for the caller's (tenant, user), exactly the server's
    key minus the context hint, which hosted MCP never sends. It survives any number of MCP sessions and middleware
    instances, as the Mongo record does. `forbidden` workspaces answer 403 (not a member); `bound` bearers answer 409.

    Route policy (review M1): a header-less call resolves through the levers only where the real service runs the
    resolver. `pre_landed=True` (the default) is the TARGET behaviour of WS-ROUTING-5-PRE-1, NOT LANDED at fix round 1:
    every data route in `MCP_ROUTE_POLICY` resolves (call > session > floor), and with the floor flag off the floor is
    the caller's `default_team_id` (`routed_by = account_default`, contract `flags.ROUTING_PERSONAL_FLOOR.off`), else
    400. `pre_landed=False` is today's service: user_only routes never resolve and the flag-off floor is always 400.
    Both floors are stamped `account_default` (common `resolver.py:432-435`; there is no "floor" value).
    """

    # The contract's session store (`ws-routing-contract.json` `stores.session`): idle TTL 24 h, pushed forward on a
    # resolved write at most once per hour.
    IDLE_TTL_S = 86400.0
    REFRESH_EVERY_S = 3600.0

    def __init__(
        self,
        *,
        forbidden: tuple[str, ...] = (),
        bound: tuple[str, ...] = (),
        personal_floor: bool = False,
        pre_landed: bool = True,
        default_team_id: str | None = None,
    ) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        # (bearer, client_key) -> (expires_at, refreshed_at) on `now`. A row written straight into `rows` never expires.
        self.expiry: dict[tuple[str, str], tuple[float, float]] = {}
        self.forbidden, self.bound = set(forbidden), set(bound)
        self.personal_floor = personal_floor
        self.pre_landed = pre_landed
        self.default_team_id = default_team_id
        # The design's agent case: a header-less call no lever answers is refused even with the floor on.
        self.refuse_headerless = False
        self.now = 0.0  # the fake clock, in seconds; tests advance it
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        # Every data-route call the resolver answered: (method, path, workspace_id or None, routed_by or refusal).
        self.resolved: list[tuple[str, str, str | None, str]] = []

    def _live_session(self, key: tuple[str, str]) -> str | None:
        """The session pick for `key` when it has not lapsed on the fake clock, else None (the row is dropped)."""
        if key not in self.rows:
            return None
        expires_at, _ = self.expiry.get(key, (float("inf"), 0.0))
        if self.now >= expires_at:
            del self.rows[key]
            self.expiry.pop(key, None)
            return None
        return self.rows[key]

    def floor(self) -> str | None:
        """The floor a header-less call with no lever lands on, or None (refused 400 missing_team_context)."""
        if self.refuse_headerless:
            return None
        if self.personal_floor:
            return "ws_personal_user-1"
        return self.default_team_id if self.pre_landed else None

    def resolve(
        self, method: str, path: str, headers: dict[str, str]
    ) -> tuple[str | None, str]:
        """The common resolver, cut to the levers the MCP reaches: call header, session (by `X-SM-Client`), floor.

        Returns (workspace_id, routed_by), or (None, "missing_team_context") for a refusal. A session-routed write
        (not GET/HEAD/OPTIONS) refreshes the row when more than an hour passed since the last refresh, and never
        revives a lapsed row (common `scope.py:590-595`, `routing/sessions.py:204-222`).
        """
        bearer = str(headers.get("Authorization", "")).removeprefix("Bearer ")
        header = headers.get("X-Workspace-Id")
        policy = route_policy(method, path)
        if header:
            answer: tuple[str | None, str] = (header, "call")
        elif policy == USER_ONLY and not self.pre_landed:
            answer = (
                None,
                "missing_team_context",
            )  # today: user_only never runs the resolver (M1)
        else:
            key = (bearer, str(headers.get("X-SM-Client") or ""))
            chosen = self._live_session(key) if key[1] else None
            floor = self.floor()
            if chosen:
                answer = (chosen, "session")
                if (
                    method.upper() not in ("GET", "HEAD", "OPTIONS")
                    and key in self.expiry
                ):
                    expires_at, refreshed_at = self.expiry[key]
                    if self.now - refreshed_at > self.REFRESH_EVERY_S:
                        self.expiry[key] = (self.now + self.IDLE_TTL_S, self.now)
            elif floor:
                answer = (floor, "account_default")
            else:
                answer = (None, "missing_team_context")
        self.resolved.append((method.upper(), path, answer[0], answer[1]))
        return answer

    def svc_api(self, fallback):
        """An `httpx.request`-shaped handler: routing routes answered here, data routes resolved then `fallback`.

        A header-less call that no lever answers gets the flag-off refusal (400 + `X-SM-Scope-Error`), as svc-api's
        scope dependency gives it before any route runs. A resolved write's JSON object answer carries the contract's
        `write_response_echo` fields.
        """

        def handler(method: str, url: str, **kwargs: Any) -> httpx.Response:
            headers = dict(kwargs.get("headers") or {})
            params = kwargs.get("params")
            request = httpx.Request(
                method, url, headers=headers, params=params, json=kwargs.get("json")
            )
            routed = self.handle(request)
            if routed is not None:
                routed.request = request
                return routed
            path = request.url.path
            if not path.startswith("/memory/") or (
                route_policy(method, path) not in DATA_POLICIES
            ):
                return fallback(method, url, **kwargs)
            workspace_id, routed_by = self.resolve(method, path, headers)
            if workspace_id is None:
                return httpx.Response(
                    400,
                    json={"detail": {"error": routed_by}},
                    headers={"X-SM-Scope-Error": routed_by},
                    request=request,
                )
            response = fallback(method, url, **kwargs)
            if method.upper() == "GET" or response.is_error:
                return response
            try:
                body = response.json()
            except ValueError:
                return response
            if not isinstance(body, dict):
                return response
            body = {**body, "workspace_id": workspace_id, "routed_by": routed_by}
            return httpx.Response(
                response.status_code,
                json=body,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in ("content-length", "content-type")
                },
                request=request,
            )

        return handler

    def install(self, monkeypatch, fallback) -> None:
        """Route `RemoteBackend`'s module-level `httpx.request` and `httpx.get` (explain) through `svc_api(fallback)`."""
        handler = self.svc_api(fallback)
        monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", handler)
        monkeypatch.setattr(
            "smartmemory_mcp.backends.remote.httpx.get",
            lambda url, **kwargs: handler("GET", url, **kwargs),
        )

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
            key = (bearer, body["client_key"])
            self.rows[key] = body["workspace_id"]
            self.expiry[key] = (self.now + self.IDLE_TTL_S, self.now)
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
            chosen = self._live_session((self._bearer(request), client_key))
            floor = self.floor()
            candidate = {
                "lever": "session",
                "workspace_id": chosen,
                "status": "chosen" if chosen else "absent",
            }
            return httpx.Response(
                200,
                json={
                    "workspace_id": chosen or floor,
                    "routed_by": "session"
                    if chosen
                    else ("account_default" if floor else None),
                    "candidates": [
                        {"lever": "call", "workspace_id": None, "status": "absent"},
                        candidate,
                    ],
                    "flags": {"personal_floor": self.personal_floor},
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
