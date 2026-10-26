"""The mcp suite never reaches the shared FalkorDB 9010, Redis 9012 or Mongo 9013 (MCP-TEST-SHARED-DB-GUARD-1)."""

import json
import logging
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests import conftest as guard
from tests._falkor import ALLOW_SHARED_ENV, SHARED_PORTS

TESTS = Path(__file__).parent
ROOT = TESTS.parent
FALKOR_FILES = [
    "tests/integration/test_hold_receipt.py",
    "tests/test_code_disambiguation.py",
    "tests/test_framework_semantics.py",
]
# Runs pytest in a child with every connect recorded (the guard is opted out so the recorder, not the guard, sees it).
CHILD = """
import json, socket, sys
import pytest
seen = []
def connect(self, address):
    seen.append(list(address[:2]) if isinstance(address, tuple) else str(address))
    raise ConnectionRefusedError("recorded")
socket.socket.connect = connect
code = pytest.main(sys.argv[2:] + ["-q", "-p", "no:cacheprovider"])
json.dump({"code": int(code), "seen": seen}, open(sys.argv[1], "w"))
"""


def _child(tmp_path, args):
    out = tmp_path / "seen.json"
    env = {k: v for k, v in __import__("os").environ.items() if k not in {"FALKORDB_PORT", "GRAPH_DB_PORT"}}
    env[ALLOW_SHARED_ENV] = "1"
    run = subprocess.run(
        [sys.executable, "-c", CHILD, str(out), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.exists(), run.stdout[-800:] + run.stderr[-800:]
    return json.loads(out.read_text()), run.stdout


def _refused(port, caplog):
    guard._warned.clear()
    with caplog.at_level(logging.WARNING, logger=guard._log.name):
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("localhost", port), timeout=1)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(port) in warnings[0], warnings


def _opt_in(port, monkeypatch):
    monkeypatch.setenv(ALLOW_SHARED_ENV, "1")
    assert guard.shared_port(("localhost", port)) is None


def _remote_host(port, monkeypatch):
    monkeypatch.delenv(ALLOW_SHARED_ENV, raising=False)
    assert guard.shared_port(("db.example.com", port)) is None


def _private_port(port, monkeypatch):
    """A private server on any other port still connects."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        threading.Thread(target=server.accept, daemon=True).start()
        socket.create_connection(server.getsockname(), timeout=2).close()


def _endpoint_default(port, monkeypatch):
    from tests._falkor import falkor_port

    for name in ("FALKORDB_PORT", "GRAPH_DB_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert falkor_port() == 1
    monkeypatch.setenv("GRAPH_DB_PORT", "19123")
    assert falkor_port() == 19123


def _no_literal_port(port, monkeypatch):
    allowed = {"conftest.py", "_falkor.py", Path(__file__).name}
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in TESTS.rglob("*.py")
        if p.name not in allowed
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(rf"\b{port}\b", line)
    ]
    assert hits == []


def _collect_and_run_dial_nothing_shared(port, monkeypatch, tmp_path=None):
    seen, out = _child(tmp_path, ["--collect-only", *FALKOR_FILES])
    assert seen["code"] == 0, out[-600:]
    assert not [a for a in seen["seen"] if a[1] in SHARED_PORTS], seen["seen"]
    seen, out = _child(tmp_path, [*FALKOR_FILES, "-rs"])
    assert not [a for a in seen["seen"] if a[1] in SHARED_PORTS], seen["seen"]
    # a row ends passed or skipped, never as an error that came from the refused dial
    failed = [l for l in out.splitlines() if l.startswith(("ERROR", "FAILED")) and "recorded" in l]
    assert failed == [] and "SKIPPED" in out, out[-800:]


ROWS = [
    ("refuse-9010", 9010, _refused),
    ("refuse-9012", 9012, _refused),
    ("refuse-9013", 9013, _refused),
    ("opt-in-9010", 9010, _opt_in),
    ("remote-host-9010", 9010, _remote_host),
    ("private-port", 0, _private_port),
    ("endpoint-default-1", 0, _endpoint_default),
    ("no-literal-shared-port-9010", 9010, _no_literal_port),
    ("no-literal-shared-port-9012", 9012, _no_literal_port),
    ("no-literal-shared-port-9013", 9013, _no_literal_port),
    ("falkor-files-never-dial-shared", 9010, _collect_and_run_dial_nothing_shared),
]


@pytest.mark.parametrize("port,check", [(p, c) for _, p, c in ROWS], ids=[r[0] for r in ROWS])
def test_shared_db_guard(port, check, monkeypatch, caplog, tmp_path):
    kwargs = {"tmp_path": tmp_path} if check is _collect_and_run_dial_nothing_shared else {}
    if check is _refused:
        monkeypatch.delenv(ALLOW_SHARED_ENV, raising=False)
        check(port, caplog)
    else:
        check(port, monkeypatch, **kwargs)
