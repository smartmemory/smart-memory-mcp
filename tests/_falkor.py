"""The FalkorDB endpoint every mcp test connection uses (MCP-TEST-SHARED-DB-GUARD-1).

``FALKORDB_HOST``/``FALKORDB_PORT``, then ``GRAPH_DB_HOST``/``GRAPH_DB_PORT``, then ``localhost:1``: nothing listens
on port 1, so a run that names no private server skips its FalkorDB rows instead of dialing the shared dev graph
(9010). ``tests/conftest.py`` exports the answer into both families so the clients the product builds for itself
resolve the same endpoint, and its socket guard refuses 9010/9012/9013 for anything that still names them.
"""

from __future__ import annotations

import os

import pytest

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1
SHARED_PORTS = frozenset({9010, 9012, 9013})
ALLOW_SHARED_ENV = "SMARTMEMORY_TEST_ALLOW_SHARED_DB"
ENDPOINT_ENV = ("FALKORDB_HOST", "FALKORDB_PORT", "GRAPH_DB_HOST", "GRAPH_DB_PORT")


def _first(*names: str) -> str | None:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def falkor_host() -> str:
    return _first("FALKORDB_HOST", "GRAPH_DB_HOST") or DEFAULT_HOST


def falkor_port() -> int:
    return int(_first("FALKORDB_PORT", "GRAPH_DB_PORT") or DEFAULT_PORT)


def export_endpoint() -> tuple[str, int]:
    """Make both resolver families name this module's endpoint (the product reads either one)."""
    host, port = falkor_host(), falkor_port()
    for prefix in ("FALKORDB", "GRAPH_DB"):
        os.environ[f"{prefix}_HOST"] = host
        os.environ[f"{prefix}_PORT"] = str(port)
    return host, port


def falkor_client_or_skip():
    """A connected Redis client on the test endpoint, or a skip when nothing listens there."""
    from redis import Redis
    from redis.exceptions import RedisError

    host, port = falkor_host(), falkor_port()
    client = Redis(host=host, port=port, socket_connect_timeout=2, socket_timeout=5)
    try:
        client.ping()
    except (RedisError, OSError) as error:
        client.close()
        pytest.skip(f"FalkorDB unreachable at {host}:{port} (set FALKORDB_PORT to a private server): {error}")
    return client
