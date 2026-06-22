"""CORE-CODE-PROVENANCE-1 Phase 2c — MCP `code_read_transcript` tool.

The read half of the blame->read chain: given a `code_blame` match's read handle
(source, source_path, line_no, and for live CC file+norm_hash), render a centered
window of the authoring transcript. Local-backend only; the hosted REST surface is
parked. The reader logic itself is covered in smart-memory-core
(`test_provenance_reader.py`); here we cover the tool surface.
"""
from __future__ import annotations

from unittest.mock import patch

from smartmemory_mcp.tools import code_tools


def _registered() -> dict:
    captured: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def _decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorator

    code_tools.register(_FakeMCP())
    return captured


class _LocalBackend:
    """Local backend: has read_transcript_centered, NO `request` attribute."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.last_kwargs = None

    def read_transcript_centered(self, **kw):
        self.last_kwargs = kw
        if self._raises is not None:
            raise self._raises
        return self._result


class _RemoteBackend:
    def request(self, *a, **kw):  # pragma: no cover - never called
        return {}


def _window(**ov):
    base = {
        "window": "user: add login\n[Write] {\"file_path\": \"auth.py\"}\nresult ok",
        "handle": {"source": "cc", "source_path": "/t/s.jsonl", "session_id": "sess-A", "line_no": 2},
        "continue_cursor": {"prev_line": 1, "next_line": 3},
        "chars_used": 42,
        "char_budget": 20000,
    }
    base.update(ov)
    return base


def test_code_read_transcript_is_registered():
    assert "code_read_transcript" in _registered()


def test_local_renders_window():
    tool = _registered()["code_read_transcript"]
    be = _LocalBackend(result=_window())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=be):
        out = tool(source_path="/t/s.jsonl", line_no=2, source="cc")
    assert "add login" in out
    assert "sess-A" in out


def test_zero_line_becomes_none_and_locate_passed():
    tool = _registered()["code_read_transcript"]
    be = _LocalBackend(result=_window())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=be):
        tool(source_path="/t/s.jsonl", line_no=0, source="cc", file="auth.py", norm_hash="abc123")
    assert be.last_kwargs["line_no"] is None  # 0 → None (live CC)
    assert be.last_kwargs["locate"] == {"file": "auth.py", "norm_hash": "abc123"}


def test_real_line_no_locate():
    tool = _registered()["code_read_transcript"]
    be = _LocalBackend(result=_window())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=be):
        tool(source_path="/t/r.jsonl", line_no=3, source="codex")
    assert be.last_kwargs["line_no"] == 3
    assert be.last_kwargs["locate"] is None  # no file+hash → no locate


def test_next_line_forwards_cursor():
    tool = _registered()["code_read_transcript"]
    be = _LocalBackend(result=_window())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=be):
        tool(source_path="/t/s.jsonl", source="cc", next_line=5)
    assert be.last_kwargs["cursor"] == {"next_line": 5}
    assert be.last_kwargs["line_no"] == 5  # paging extends forward from the cursor edge


def test_prev_line_forwards_cursor():
    tool = _registered()["code_read_transcript"]
    be = _LocalBackend(result=_window())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=be):
        tool(source_path="/t/s.jsonl", source="cc", prev_line=4)
    assert be.last_kwargs["cursor"] == {"prev_line": 4}
    assert be.last_kwargs["line_no"] == 4


def test_remote_is_parked():
    tool = _registered()["code_read_transcript"]
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=_RemoteBackend()):
        out = tool(source_path="/t/s.jsonl", line_no=2)
    low = out.lower()
    assert "local" in low and "parked" in low


def test_missing_source_path_errors():
    tool = _registered()["code_read_transcript"]
    out = tool(source_path="")
    assert out.startswith("Error") and "source_path" in out


def test_unknown_source_errors():
    tool = _registered()["code_read_transcript"]
    out = tool(source_path="/t/s.jsonl", source="svn")
    assert out.startswith("Error")
