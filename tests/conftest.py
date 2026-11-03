"""mcp test configuration: never the shared local databases (MCP-TEST-SHARED-DB-GUARD-1).

Core, service and common refuse the shared FalkorDB 9010, Redis 9012 and Mongo 9013 in their conftests
(CORE-TEST-SHARED-DB-GUARD-1). This is the mcp half: the endpoint defaults to port 1 (``tests/_falkor.py``), and a
socket guard refuses a loopback connection to a shared port, logging one WARNING that names it. A test that
genuinely needs the shared graph sets ``SMARTMEMORY_TEST_ALLOW_SHARED_DB=1``.
"""

from __future__ import annotations

import logging
import os
import socket

from tests._falkor import ALLOW_SHARED_ENV, SHARED_PORTS, export_endpoint

# Product-built clients (create_server_memory, OntologyGraph) resolve these; unset they dial 9010.
export_endpoint()

LOOPBACK = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})
_log = logging.getLogger("smartmemory_mcp.tests.shared_db_guard")
_warned: set[int] = set()
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def shared_port(address) -> int | None:
    """The shared database port a socket address names, or None."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host, port = address[0], address[1]
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host not in LOOPBACK or port not in SHARED_PORTS:
        return None
    if os.environ.get(ALLOW_SHARED_ENV) == "1":
        return None
    return int(port)


def _refuse(port: int) -> ConnectionRefusedError:
    if port not in _warned:
        _warned.add(port)
        _log.warning(
            "refusing a test connection to shared database port %s: set FALKORDB_PORT (or GRAPH_DB_PORT) to a "
            "private server, or %s=1 to opt in",
            port,
            ALLOW_SHARED_ENV,
        )
    return ConnectionRefusedError(f"test connection to shared database port {port} refused ({ALLOW_SHARED_ENV}=1 allows it)")


def _guarded_connect(self, address):
    port = shared_port(address)
    if port is not None:
        raise _refuse(port)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    port = shared_port(address)
    if port is not None:
        _refuse(port)
        return 111  # ECONNREFUSED
    return _real_connect_ex(self, address)


def pytest_configure(config):
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


def pytest_unconfigure(config):
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
