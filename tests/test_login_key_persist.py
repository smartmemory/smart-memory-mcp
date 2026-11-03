"""A failed login never replaces the stored key; the key file is never wider than 0600."""

import os
import stat
import sys

import httpx
import pytest

import smartmemory_mcp.tier as tier
from smartmemory_mcp.backends import remote
from smartmemory_mcp.backends.remote import RemoteBackend

OLD = "old-key-bytes"
NEW = "new-key-bytes"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX fallback file")
    """Fallback key file in tmp, keyring forced unavailable, base URL unroutable, no network."""
    path = tmp_path / "cfg" / ".api_key"
    path.parent.mkdir()
    path.write_text(OLD, encoding="utf-8")
    monkeypatch.setattr(tier, "_KEY_FILE", path)
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setenv("SMARTMEMORY_API_URL", "http://127.0.0.1:1")
    return path


def _login(monkeypatch, get):
    from smartmemory_mcp import server

    monkeypatch.setattr(remote.httpx, "get", get)
    monkeypatch.setattr(remote.RemoteBackend, "__init__", lambda self, **kw: _init(self))
    return server.login.fn(NEW) if hasattr(server.login, "fn") else server.login(NEW)


def _init(self):
    self._api_url = "http://127.0.0.1:1"
    self._session = {"access_token": "", "refresh_token": "", "user_email": "", "team_id": "t"}


def _response(status, body):
    req = httpx.Request("GET", "http://127.0.0.1:1/auth/me")
    return httpx.Response(status, content=body, request=req)


def test_401_without_error_field_keeps_stored_key(key_file, monkeypatch):
    out = _login(monkeypatch, lambda *a, **k: _response(401, b'{"detail":"bad"}'))
    assert "validation failed (401)" in out
    assert key_file.read_text(encoding="utf-8") == OLD


def test_network_error_keeps_stored_key(key_file, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    out = _login(monkeypatch, boom)
    assert "Login failed" in out
    assert key_file.read_text(encoding="utf-8") == OLD


def test_success_replaces_stored_key(key_file, monkeypatch):
    out = _login(monkeypatch, lambda *a, **k: _response(200, b'{"email":"a@b.c"}'))
    assert "Logged in as a@b.c" in out
    assert key_file.read_text(encoding="utf-8") == NEW


def test_backend_login_result_is_typed(monkeypatch):
    monkeypatch.setattr(remote.httpx, "get", lambda *a, **k: _response(500, b"x"))
    backend = RemoteBackend(api_key="k", api_url="http://127.0.0.1:1")
    result = backend.login("k")
    assert result.ok is False and "500" in result.message


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_new_key_file_created_0600_before_any_bytes(tmp_path, monkeypatch):
    path = tmp_path / "fresh" / ".api_key"
    monkeypatch.setattr(tier, "_KEY_FILE", path)
    monkeypatch.setitem(sys.modules, "keyring", None)
    modes = []
    real_open = os.open

    def spy(p, flags, mode=0o777, **kw):
        fd = real_open(p, flags, mode, **kw)
        modes.append((stat.S_IMODE(os.fstat(fd).st_mode), os.fstat(fd).st_size))
        return fd

    monkeypatch.setattr(os, "open", spy)
    tier.store_api_key(NEW)
    assert modes == [(0o600, 0)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == NEW
    assert [p.name for p in path.parent.iterdir()] == [".api_key"]
