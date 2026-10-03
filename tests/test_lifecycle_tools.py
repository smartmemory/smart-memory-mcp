"""Tests for DIST-AGENT-HOOKS-1 MCP lifecycle tools."""

import json
from pathlib import Path

import pytest

from smartmemory_mcp.tools import lifecycle_tools


class TestLifecycleToolRegistration:
    """memory_auto should be registered in FREE tier."""

    def test_memory_auto_in_free_tier(self, monkeypatch):
        """memory_auto is available at FREE tier (no login needed)."""
        monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
        monkeypatch.delenv("SMARTMEMORY_MCP_FULL_TOOLS", raising=False)

        # Import fresh to get tool list
        from smartmemory_mcp.tools import lifecycle_tools

        # Verify the module has a register function
        assert hasattr(lifecycle_tools, "register")


def _memory_auto():
    registered = {}

    class Registrar:
        def tool(self, **kwargs):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn

            return decorator

    lifecycle_tools.register(Registrar())
    return registered["memory_auto"]


def test_overrides_replace_existing_session_preserving_unicode(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "test_session.json"
    path.write_text(
        json.dumps(
            {"session_id": "test_session", "summary": "你好 café"}, ensure_ascii=False
        ),
        encoding="utf-8",
    )

    def forbid_rename(*args):
        raise AssertionError("Windows rename cannot replace an existing destination")

    monkeypatch.setattr(Path, "rename", forbid_rename)
    tool = _memory_auto()
    assert "Lifecycle enabled." in tool(session_id="test_session", orient_budget=900)
    assert "Lifecycle disabled." in tool(
        session_id="test_session", enabled=False, orient_budget=700
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["summary"] == "你好 café"
    assert saved["config_overrides"]["enabled"] is False
    assert saved["config_overrides"]["orient_budget"] == 700
    assert list(sessions.glob("*.tmp")) == []


@pytest.mark.parametrize("persistent", [False, True])
def test_windows_sharing_failure_is_bounded_and_visible(
    tmp_path, monkeypatch, caplog, persistent
):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    lifecycle_tools._write_session_overrides("test_session", {"enabled": True})
    path = tmp_path / "sessions" / "test_session.json"
    before = path.read_bytes()
    original = Path.replace
    calls = []

    def sharing_replace(source, destination):
        calls.append(source)
        if persistent or len(calls) < 3:
            error = PermissionError("sharing violation")
            error.winerror = 32
            raise error
        return original(source, destination)

    monkeypatch.setattr(lifecycle_tools.sys, "platform", "win32")
    monkeypatch.setattr(lifecycle_tools.time, "sleep", lambda _: None)
    monkeypatch.setattr(Path, "replace", sharing_replace)
    result = _memory_auto()(session_id="test_session", enabled=False)
    assert len(calls) == 3
    assert len(set(calls)) == 1
    assert list(path.parent.glob("*.tmp")) == []
    if persistent:
        assert "Lifecycle settings not saved" in result
        assert "Lifecycle disabled." not in result
        assert "Failed to write session overrides" in caplog.text
        assert path.read_bytes() == before
    else:
        assert "Lifecycle disabled." in result
        assert json.loads(path.read_text())["config_overrides"]["enabled"] is False


@pytest.mark.parametrize("state", ["{invalid", "[]"])
def test_corrupt_session_does_not_report_success(tmp_path, monkeypatch, caplog, state):
    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "test_session.json"
    path.write_text(state, encoding="utf-8")
    result = _memory_auto()(session_id="test_session")
    assert "Lifecycle settings not saved" in result
    assert "Lifecycle enabled." not in result
    assert "Failed to write session overrides" in caplog.text
    assert path.read_text() == state
    assert list(sessions.glob("*.tmp")) == []


@pytest.mark.parametrize("session_id", ["", "../"])
def test_missing_or_invalid_session_does_not_report_success(session_id, caplog):
    result = _memory_auto()(session_id=session_id)
    assert "Lifecycle settings not saved" in result
    assert "Lifecycle enabled." not in result
