"""Backend dispatch: REST by default, in-process only on explicit opt-in (MCP-PERF-1).

One row per mode setting and service state. Every remote row asserts the in-process
store was never touched (`smartmemory_app.storage` is replaced by a module that fails
on use), because the bug this pins is "smartmemory is importable, so every MCP
process silently built a full local memory and loaded its models".
"""

from __future__ import annotations

import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import ModuleType, SimpleNamespace

import pytest

from smartmemory_mcp.backends import dispatch
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend

UNREACHABLE_URL = "http://127.0.0.1:1"  # port 1: connection refused at once


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def reachable_url():
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    dispatch.reset_backend()
    dispatch._warned.clear()
    for name in ("SMARTMEMORY_MCP_MODE", "SMARTMEMORY_API_URL", "SMARTMEMORY_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "sk_test_dispatch")
    storage = ModuleType("smartmemory_app.storage")

    def _no_local_store(*args, **kwargs):
        raise AssertionError("in-process store opened")

    storage.get_memory = _no_local_store
    monkeypatch.setitem(sys.modules, "smartmemory_app.storage", storage)
    yield
    dispatch.reset_backend()
    dispatch._warned.clear()


def _app_config(monkeypatch, mode):
    """Install a fake smartmemory CLI config (mode None = package absent)."""
    if mode is None:
        monkeypatch.setitem(sys.modules, "smartmemory_app.config", None)  # ImportError
        return
    cfg = SimpleNamespace(mode=mode, api_url="", team_id="")
    module = ModuleType("smartmemory_app.config")
    module.load_config = lambda: cfg
    monkeypatch.setitem(sys.modules, "smartmemory_app.config", module)


# (row id, SMARTMEMORY_MCP_MODE, CLI config mode, service, backend, warning substrings)
ROWS = [
    ("unset+reachable", None, "local", "up", RemoteBackend, ["config says mode=local"]),
    (
        "unset+unreachable",
        None,
        None,
        "down",
        RemoteBackend,
        ["unreachable at http://127.0.0.1:1"],
    ),
    ("remote", "remote", "remote", "up", RemoteBackend, []),
    (
        "remote-uppercase+down",
        " REMOTE ",
        "local",
        "down",
        RemoteBackend,
        ["unreachable at http://127.0.0.1:1", "config says mode=local"],
    ),
    ("local opt-in", "local", None, "down", LocalBackend, []),
    (
        "garbage",
        "lcoal",
        "local",
        "up",
        RemoteBackend,
        ["'lcoal' is not one of local|remote|hosted", "config says mode=local"],
    ),
]


@pytest.mark.parametrize(
    "mode_env,cfg_mode,service,expected,warnings",
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_mode_table(
    monkeypatch, caplog, reachable_url, mode_env, cfg_mode, service, expected, warnings
):
    url = reachable_url if service == "up" else UNREACHABLE_URL
    monkeypatch.setenv("SMARTMEMORY_API_URL", url)
    if mode_env is not None:
        monkeypatch.setenv("SMARTMEMORY_MCP_MODE", mode_env)
    _app_config(monkeypatch, cfg_mode)

    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.backends.dispatch"):
        backend = dispatch.resolve_backend()
        probe = dispatch.start_reachability_probe()
        if probe is not None:
            probe.join(10)

    assert type(backend) is expected
    if expected is RemoteBackend:
        assert backend._api_url == url
        assert probe is not None
    else:
        assert probe is None  # no REST probe in local mode
        assert backend._memory is None  # opt-in still loads nothing until first use
    text = "\n".join(r.getMessage() for r in caplog.records)
    for fragment in warnings:
        assert fragment in text
    assert len(caplog.records) == len(warnings), text


def test_unreachable_warns_once_per_url(caplog):
    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.backends.dispatch"):
        assert dispatch.check_service_reachable(UNREACHABLE_URL) is False
        assert dispatch.check_service_reachable(UNREACHABLE_URL) is False
    assert len(caplog.records) == 1
    assert "SMARTMEMORY_MCP_MODE=local" in caplog.records[0].getMessage()


def test_unreachable_remote_tool_call_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_API_URL", UNREACHABLE_URL)
    _app_config(monkeypatch, "local")
    result = dispatch.resolve_backend()._request("GET", "/memory/health")
    assert "unreachable at http://127.0.0.1:1" in result["error"]


def test_no_api_key_and_no_package_names_both_ways_out(monkeypatch):
    monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "")
    _app_config(monkeypatch, None)
    with pytest.raises(RuntimeError, match="SMARTMEMORY_MCP_MODE=local"):
        dispatch.resolve_backend()


def test_no_api_key_with_package_is_unauthenticated_rest(monkeypatch, caplog):
    """tools/list must still work, so no raise; and still no in-process store."""
    monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "")
    monkeypatch.setenv("SMARTMEMORY_API_URL", UNREACHABLE_URL)
    _app_config(monkeypatch, "remote")
    with caplog.at_level(logging.WARNING, logger="smartmemory_mcp.backends.dispatch"):
        backend = dispatch.resolve_backend()
    assert isinstance(backend, RemoteBackend)
    assert "Not authenticated" in backend.whoami()
    assert "No SmartMemory API key" in caplog.text


def test_backend_is_cached_and_reset(monkeypatch, reachable_url):
    monkeypatch.setenv("SMARTMEMORY_API_URL", reachable_url)
    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", "remote")  # tracked, so restored after
    _app_config(monkeypatch, None)
    first = dispatch.resolve_backend()
    assert dispatch.resolve_backend() is first
    dispatch.set_process_mode("local")
    assert isinstance(dispatch.resolve_backend(), LocalBackend)
