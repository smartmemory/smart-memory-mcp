"""In-process rate limiting for the hosted ASGI app (S4, design.md §5).

FastMCP permits unlimited dynamic client registration and the OAuth endpoints
are unauthenticated by construction, so the public surface needs a budget of its
own. This is deliberately in-process and best-effort: it is a flood brake in
front of Redis and Clerk, not an accounting system.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import HostedConfig

logger = logging.getLogger(__name__)

MINUTE = 60.0
HOUR = 3600.0

REGISTER_PER_IP_PER_MINUTE = 10
REGISTER_GLOBAL_PER_HOUR = 200
OAUTH_PER_IP_PER_MINUTE = 30
UNAUTHENTICATED_MCP_PER_IP_PER_MINUTE = 60

UNKNOWN_CLIENT = "unknown"


@dataclass(frozen=True)
class _Rule:
    """One budget: `limit` events per `window` seconds under a bucket name."""

    name: str
    limit: int
    window: float


REGISTER_RULE = _Rule("register", REGISTER_PER_IP_PER_MINUTE, MINUTE)
REGISTER_GLOBAL_RULE = _Rule("register-global", REGISTER_GLOBAL_PER_HOUR, HOUR)
OAUTH_RULE = _Rule("oauth", OAUTH_PER_IP_PER_MINUTE, MINUTE)
MCP_RULE = _Rule("mcp-unauthenticated", UNAUTHENTICATED_MCP_PER_IP_PER_MINUTE, MINUTE)


class _FixedWindowCounter:
    """Per-bucket fixed-window counters. Thread-safe; entries expire lazily."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # bucket -> (window_started_at, count)
        self._buckets: dict[str, tuple[float, int]] = {}

    def hit(self, rule: _Rule, key: str) -> float | None:
        """Record one event. Returns seconds to wait when over budget, else None."""
        bucket = f"{rule.name}:{key}"
        now = self._clock()
        with self._lock:
            started_at, count = self._buckets.get(bucket, (now, 0))
            if now - started_at >= rule.window:
                started_at, count = now, 0
            if count >= rule.limit:
                self._buckets[bucket] = (started_at, count)
                return max(1.0, rule.window - (now - started_at))
            self._buckets[bucket] = (started_at, count + 1)
            return None


class HostedRateLimit(BaseHTTPMiddleware):
    """Budget the unauthenticated public surface by client IP.

    Constructed only through the `Middleware(HostedRateLimit, config=cfg)`
    descriptor that `hosted_asgi_middleware` returns — Starlette instantiates it
    with the app as the first argument (`mixins/transport.py:258-268`).
    """

    def __init__(self, app, config: HostedConfig, clock=time.monotonic) -> None:
        super().__init__(app)
        self._trust_proxy = config.trust_proxy
        self._counter = _FixedWindowCounter(clock=clock)

    def client_ip(self, request: Request) -> str:
        """The caller's address.

        `X-Forwarded-For` is honoured ONLY when the config says this process sits
        behind our own reverse proxy. Trusting it unconditionally would let any
        caller mint a fresh bucket per request by varying the header.
        """
        if self._trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        client = request.client
        return client.host if client is not None else UNKNOWN_CLIENT

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path.rstrip("/") or "/"
        ip = self.client_ip(request)

        if path.endswith("/register"):
            retry_after = self._counter.hit(REGISTER_GLOBAL_RULE, "all")
            if retry_after is not None:
                # The one budget whose exhaustion is a signal rather than noise:
                # it means someone is filling the client store, not that one
                # client is chatty.
                logger.warning(
                    "Hosted registration budget exhausted (%d/hour); refusing "
                    "registration from %s.",
                    REGISTER_GLOBAL_PER_HOUR,
                    ip,
                )
                return _too_many(retry_after)
            retry_after = self._counter.hit(REGISTER_RULE, ip)
        elif path.endswith("/authorize") or path.endswith("/token"):
            # One budget per endpoint, not one shared between them: a single
            # legitimate authorization is one /authorize AND one /token, so a
            # shared bucket would halve the real flow rate.
            retry_after = self._counter.hit(OAUTH_RULE, f"{path}|{ip}")
        elif path.endswith("/mcp") and not request.headers.get("authorization"):
            retry_after = self._counter.hit(MCP_RULE, ip)
        else:
            retry_after = None

        if retry_after is not None:
            logger.info(
                "Rate limited %s on %s; retry after %.0fs.", ip, path, retry_after
            )
            return _too_many(retry_after)

        return await call_next(request)


def _too_many(retry_after: float) -> JSONResponse:
    seconds = str(int(retry_after))
    return JSONResponse(
        {
            "error": "rate_limited",
            "error_description": f"Too many requests; retry after {seconds} seconds.",
        },
        status_code=429,
        headers={"Retry-After": seconds},
    )


def hosted_asgi_middleware(cfg: HostedConfig) -> list[Middleware]:
    """The ASGI middleware stack for the single served hosted app.

    FastMCP takes descriptors, not instances (`mixins/transport.py:258-268`):
    `BaseHTTPMiddleware` needs `app` as its first argument, so an instance
    cannot be passed here.
    """
    return [Middleware(HostedRateLimit, config=cfg)]
