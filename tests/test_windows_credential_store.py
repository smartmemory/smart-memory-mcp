"""Shared and standalone Windows fallback credential acceptance regressions."""

import builtins
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import keyring
import pytest
from keyring.backends.fail import Keyring

from smartmemory_app import config, windows_credentials as app_store
from smartmemory_mcp import tier, windows_credentials as credential_store

SID = "S-1-5-21-123"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(Keyring())
    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(tier, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        credential_store, "key_path", lambda: tmp_path / "shared" / "api_key"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(
        tier, "_KEY_FILE", tmp_path / "home/.config/smartmemory/.api_key"
    )
    monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
    try:
        yield app_store.key_path(), tier._KEY_FILE
    finally:
        keyring.set_keyring(previous)


def test_wrapper_mcp_rotation_and_legacy_migration(isolated, monkeypatch):
    shared, legacy = isolated
    # The native permission mechanism is separately proved on TVPC.
    # This portable seam exercises both real readers/writers and real atomic files.
    import smartmemory_mcp.windows_credentials as store

    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    monkeypatch.setattr(store, "_restrict", lambda path, sid: None)
    legacy.parent.mkdir(parents=True)
    legacy.write_text("test_legacy")
    assert config.get_api_key() == tier.get_api_key() == "test_legacy"
    assert not legacy.exists()
    with pytest.warns(UserWarning):
        config.set_api_key("test_A")
    tier.store_api_key("test_B")
    assert config.get_api_key() == tier.get_api_key() == "test_B"
    with pytest.warns(UserWarning):
        config.set_api_key("test_C")
    assert config.get_api_key() == tier.get_api_key() == "test_C"
    legacy.write_text("test_stale")
    older = shared.stat().st_mtime_ns - 2_000_000_000
    os.utime(legacy, ns=(older, older))
    assert tier.get_api_key() == "test_C"
    assert not legacy.exists()
    assert shared.read_text() == "test_C"


def test_standalone_mcp_uses_same_secure_implementation(isolated, monkeypatch):
    shared, legacy = isolated
    import smartmemory_mcp.windows_credentials as store

    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    monkeypatch.setattr(store, "_restrict", lambda path, sid: None)
    original = builtins.__import__

    def without_wrapper(name, *args, **kwargs):
        if name.startswith("smartmemory_app"):
            raise ModuleNotFoundError(name, name=name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_wrapper)
    tier.store_api_key("test_standalone")
    assert tier.get_api_key() == "test_standalone"
    # Intended contract change: wrapper importability never selects a second store.
    assert shared.read_text() == "test_standalone"
    assert not legacy.exists()
    monkeypatch.setattr(keyring, "get_password", lambda *args: "test_keyring")
    assert tier.get_api_key() == "test_keyring"


def test_two_interpreter_rotation_without_wrapper(tmp_path):
    """Run real entry points with the wrapper absent from the standalone import path."""
    root = Path(__file__).resolve().parents[2]
    dependencies = [p for p in sys.path if "site-packages" in p]
    env = os.environ.copy()
    env.pop("SMARTMEMORY_API_KEY", None)
    env.pop("PYTHONPATH", None)
    env.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        APPDATA=str(tmp_path / "appdata"),
        SMARTMEMORY_CRASH_REPORTS="0",
        SMARTMEMORY_NO_UPDATE_CHECK="1",
    )
    (tmp_path / "home").mkdir()
    script = r"""
import importlib.util, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = __PATHS__
import keyring
from keyring.backends.fail import Keyring
keyring.set_keyring(Keyring())
from smartmemory_mcp import windows_credentials as store
# Portable filesystem/locking regression. Real SID/DACL proof runs on TVPC.
store._current_sid = lambda: "S-1-5-21-123"
store._restrict = lambda path, sid: None
actor, action, value = sys.argv[1:]
if actor == "wrapper":
    from smartmemory_app import config as api
    api.sys = SimpleNamespace(platform="win32")
    api.config_path = lambda: Path(os.environ["APPDATA"]) / "smartmemory/config.toml"
    read, write = api.get_api_key, api.set_api_key
else:
    assert importlib.util.find_spec("smartmemory_app") is None
    from smartmemory_mcp import tier as api
    api.sys = SimpleNamespace(platform="win32")
    read, write = api.get_api_key, api.store_api_key
if action == "store":
    write(value)
else:
    assert read() == value, (actor, read(), value)
"""

    def invoke(actor, action, value):
        paths = [str(root / "smart-memory-mcp"), *dependencies]
        if actor == "wrapper":
            paths.insert(0, str(root / "smart-memory"))
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                script.replace("__PATHS__", repr(paths)),
                actor,
                action,
                value,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    invoke("wrapper", "store", "test_E_A")
    invoke("mcp", "store", "test_E_B")
    invoke("wrapper", "read", "test_E_B")
    invoke("mcp", "read", "test_E_B")
    invoke("wrapper", "store", "test_E_C")
    invoke("mcp", "read", "test_E_C")
    canonical = tmp_path / "appdata/smartmemory/credentials/api_key"
    legacy = tmp_path / "home/.config/smartmemory/.api_key"
    assert canonical.read_text() == "test_E_C"
    assert canonical.with_name("api_key.lock").exists()
    assert not legacy.with_name(".api_key.lock").exists()
    # An older installation's newest successful write must survive migration.
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("test_E_legacy_newer")
    newer = canonical.stat().st_mtime_ns + 2_000_000_000
    os.utime(legacy, ns=(newer, newer))
    invoke("mcp", "read", "test_E_legacy_newer")
    invoke("wrapper", "read", "test_E_legacy_newer")
    assert not legacy.exists()


def test_newer_legacy_acl_failure_preserves_both_files(isolated, monkeypatch):
    shared, legacy = isolated
    import smartmemory_mcp.windows_credentials as store

    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    monkeypatch.setattr(store, "_restrict", lambda path, sid: None)
    app_store.store_key("test_E_canonical")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("test_E_newer")
    newer = shared.stat().st_mtime_ns + 2_000_000_000
    os.utime(legacy, ns=(newer, newer))

    def denied(path, sid):
        if path == legacy or path.name.startswith(".api_key-"):
            raise store.CredentialProtectionError("API key not persisted: ACL denied")

    monkeypatch.setattr(store, "_restrict", denied)
    with pytest.raises(OSError, match="not persisted"):
        app_store.store_key("test_E_not_persisted")
    assert shared.read_text() == "test_E_canonical"
    assert legacy.read_text() == "test_E_newer"
    assert not list(shared.parent.glob(".api_key-*"))


def test_newer_legacy_is_migrated_under_canonical_lock(isolated, monkeypatch):
    shared, legacy = isolated
    import smartmemory_mcp.windows_credentials as store

    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    monkeypatch.setattr(store, "_restrict", lambda path, sid: None)
    app_store.store_key("test_E_canonical")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("test_E_newer")
    newer = shared.stat().st_mtime_ns + 2_000_000_000
    os.utime(legacy, ns=(newer, newer))
    assert app_store.read_key() == "test_E_newer"
    assert shared.read_text() == "test_E_newer"
    assert not legacy.exists()


def test_acl_verified_on_empty_temp_before_each_write(isolated, monkeypatch):
    import smartmemory_mcp.windows_credentials as store

    shared, _ = isolated
    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    seen = []

    def restrict(path, sid):
        assert sid == SID
        if path.is_file() and path != shared:
            assert path.read_bytes() == b""
            seen.append(path)

    monkeypatch.setattr(store, "_restrict", restrict)
    app_store.store_key("test_A")
    app_store.store_key("test_B")
    assert shared.read_text() == "test_B"
    assert len(seen) == 2 and seen[0] != seen[1]
    assert not any(path.exists() for path in seen)


@pytest.mark.parametrize("kind", ["command", "unprotected", "foreign", "inherited"])
def test_acl_failure_preserves_previous_key_and_leaves_no_new_secret(
    isolated, monkeypatch, kind
):
    import smartmemory_mcp.windows_credentials as store

    shared, _ = isolated
    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    monkeypatch.setattr(store, "_restrict", lambda path, sid: None)
    app_store.store_key("test_A")
    monkeypatch.undo()

    # Use the generic store directly, so this failure does not depend on wrapper configuration.
    def acl(args, **kwargs):
        assert "/reset" not in args
        path = Path(kwargs["env"]["SMARTMEMORY_CREDENTIAL_PATH"])
        if path.is_dir():
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "protected": True,
                        "sid": SID,
                        "count": 1,
                        "inherited": False,
                        "full_control": True,
                        "allow": True,
                    }
                )
            )
        assert path.read_bytes() == b""
        if kind == "command":
            raise subprocess.CalledProcessError(5, args)
        payload = {
            "protected": kind != "unprotected",
            "sid": "S-1-1-0" if kind == "foreign" else SID,
            "count": 1,
            "inherited": kind == "inherited",
            "full_control": True,
            "allow": True,
        }
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    monkeypatch.setattr(store.subprocess, "run", acl)
    with pytest.raises(OSError, match="not persisted"):
        store.store_key(shared, "test_B")
    assert shared.read_text() == "test_A"
    assert sorted(p.name for p in shared.parent.iterdir()) == [
        "api_key",
        "api_key.lock",
    ]


def test_concurrent_writers_are_serialized(isolated, monkeypatch):
    import smartmemory_mcp.windows_credentials as store
    import threading
    import time

    shared, _ = isolated
    monkeypatch.setattr(store, "_current_sid", lambda: SID)
    count = 0
    maximum = 0
    guard = threading.Lock()

    def restrict(path, sid):
        nonlocal count, maximum
        if path.is_dir():
            return
        with guard:
            count += 1
            maximum = max(maximum, count)
        assert path.read_bytes() == b""
        time.sleep(0.03)
        with guard:
            count -= 1

    monkeypatch.setattr(store, "_restrict", restrict)
    keys = [f"test_writer_{i}" for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda key: store.store_key(shared, key), keys))
    assert maximum == 1
    assert shared.read_text() in keys
    assert sorted(p.name for p in shared.parent.iterdir()) == [
        "api_key",
        "api_key.lock",
    ]
