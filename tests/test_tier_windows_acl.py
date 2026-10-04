"""Credential precedence and fail-closed Windows fallback selection."""

from types import SimpleNamespace

import keyring
from keyring.backends.fail import Keyring
import pytest

from smartmemory_mcp import tier, windows_credentials


@pytest.fixture
def fallback_file(tmp_path, monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(Keyring())
    monkeypatch.setattr(tier, "_KEY_FILE", tmp_path / ".api_key")
    monkeypatch.setattr(windows_credentials, "key_path", lambda: tier._KEY_FILE)
    monkeypatch.setattr(
        windows_credentials, "legacy_key_path", lambda: tmp_path / "legacy"
    )
    monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
    try:
        yield tier._KEY_FILE
    finally:
        keyring.set_keyring(previous)


def test_windows_acl_failure_writes_nothing(fallback_file, monkeypatch):
    monkeypatch.setattr(tier, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_credentials, "_current_sid", lambda: "S-1-5-21-123")

    def denied(path, sid):
        raise windows_credentials.CredentialProtectionError(
            "API key not persisted: ACL denied"
        )

    monkeypatch.setattr(windows_credentials, "_restrict", denied)
    with pytest.raises(OSError, match="not persisted"):
        tier.store_api_key("test_dummy_D")
    assert not fallback_file.exists()


def test_wrapper_acl_failure_never_selects_legacy(fallback_file, monkeypatch, caplog):
    monkeypatch.setattr(tier, "sys", SimpleNamespace(platform="win32"))
    fallback_file.write_text("test_stale")

    def denied(*args):
        raise OSError("Protected credential ACL unavailable")

    monkeypatch.setattr(windows_credentials, "_current_sid", denied)
    assert tier.get_api_key() == ""
    assert "Protected credential unavailable" in caplog.text
    with pytest.raises(OSError):
        tier.store_api_key("test_new")
    assert fallback_file.read_text() == "test_stale"


def test_keyring_still_precedes_file(fallback_file, monkeypatch):
    stored = []
    monkeypatch.setattr(keyring, "set_password", lambda *args: stored.append(args))
    monkeypatch.setattr(keyring, "get_password", lambda *args: "test_dummy_D")
    tier.store_api_key("test_dummy_D")
    assert stored == [("smartmemory", "api_key", "test_dummy_D")]
    assert tier.get_api_key() == "test_dummy_D"
    assert not fallback_file.exists()


def test_posix_fallback_mode_is_private(fallback_file, monkeypatch):
    monkeypatch.setattr(tier, "sys", SimpleNamespace(platform="darwin"))
    tier.store_api_key("test_dummy_D")
    assert fallback_file.stat().st_mode & 0o777 == 0o600
