"""CORE-CODE-PROVENANCE-1 Phase 2b — standalone MCP `code_blame` tool.

Sibling to ``test_read_around_tool.py``: invokes the registered ``code_blame``
tool via a capturing fake MCP (no FastMCP server) and asserts registration, the
local-backend string renderer, the remote-path deferral message, the git_error
mapping, and argument validation. The blame logic itself is covered in
smart-memory-core (``test_provenance_blame.py``); here we cover the tool surface.

Fake backends are explicit classes (not MagicMock) so ``hasattr(backend,
"request")`` is controlled — a MagicMock auto-creates ``request`` and would always
take the remote branch.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

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
    """A local backend: has blame_code, NO `request` attribute."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises

    def blame_code(self, **kw):
        if self._raises is not None:
            raise self._raises
        return self._result


class _RemoteBackend:
    """A remote backend: has `request` (the remote-path discriminator)."""

    def request(self, *a, **kw):  # pragma: no cover - never called in 2b
        return {}


def _result(**ov):
    base = {
        "query": {"mode": "commit", "commit": "abc1234567", "repo": "/r", "ref": "HEAD"},
        "status": "ok",
        "matches": [
            {
                "source": "cc", "session_id": "sess-A", "source_path": "/t/sess-A.jsonl",
                "line_no": None, "handle": {}, "method": "exact", "score": 1.0, "jaccard": None,
                "target_coverage": 1.0, "authored_spans": [], "survival": {"overall": 1.0, "by_file": []},
                "evidence": [], "repo_unconfirmed": False,
                "read_handle": {
                    "source": "cc", "source_path": "/t/sess-A.jsonl", "session_id": "sess-A",
                    "line_no": None, "locate": {"file": "auth.py", "norm_hash": "abc123"},
                },
            }
        ],
        "ambiguous_spans": [],
        "ranked_by": "evidence_tier_then_coverage",
    }
    base.update(ov)
    return base


def test_code_blame_is_registered():
    assert "code_blame" in _registered()


def test_local_path_renders_matches():
    tool = _registered()["code_blame"]
    backend = _LocalBackend(result=_result())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=backend):
        out = tool(commit="abc1234567", repo="/r")
    assert "sess-A" in out
    assert "exact" in out
    assert "survival 100%" in out


def test_remote_path_is_parked():
    tool = _registered()["code_blame"]
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=_RemoteBackend()):
        out = tool(commit="abc1234567", repo="/r")
    low = out.lower()
    # Phase 2c parks the hosted REST surface — the message says local-only/parked,
    # not the old "the REST surface is Phase 2c" (which implied it was coming).
    assert "local" in low and "parked" in low


def test_local_path_emits_read_handle():
    # The blame match must surface a copyable code_read_transcript(...) read handle so
    # the user can chain to the reader — for live CC (line_no None) it carries locate.
    tool = _registered()["code_blame"]
    backend = _LocalBackend(result=_result())
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=backend):
        out = tool(commit="abc1234567", repo="/r")
    assert "code_read_transcript(" in out
    assert "norm_hash" in out  # live-CC match → locate{file,norm_hash} in the read call


def test_real_line_read_handle_has_no_locate():
    tool = _registered()["code_blame"]
    m = _result()
    m["matches"][0]["read_handle"] = {
        "source": "codex", "source_path": "/t/r.jsonl", "session_id": "cs1", "line_no": 3,
    }
    backend = _LocalBackend(result=m)
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=backend):
        out = tool(commit="abc1234567", repo="/r")
    assert "code_read_transcript(" in out and "line_no=3" in out
    assert "norm_hash" not in out


def test_git_error_is_graceful():
    tool = _registered()["code_blame"]
    backend = _LocalBackend(raises=ValueError("unknown commit or not a git repo: zzz"))
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=backend):
        out = tool(commit="zzz", repo="/r")
    assert out.startswith("Error (git):")


def test_no_match_is_honest():
    tool = _registered()["code_blame"]
    backend = _LocalBackend(result=_result(status="no_overlap", matches=[]))
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=backend):
        out = tool(commit="abc1234567", repo="/r")
    assert "No captured session" in out


def test_no_clear_author_flags_unconfirmed():
    tool = _registered()["code_blame"]
    m = _result(status="no_clear_author")
    m["matches"][0]["repo_unconfirmed"] = True
    backend = _LocalBackend(result=m)
    with patch("smartmemory_mcp.tools.code_tools.get_backend", return_value=backend):
        out = tool(commit="abc1234567", repo="/r")
    assert "no clear author" in out
    assert "repo-unconfirmed" in out


def test_missing_repo_errors():
    tool = _registered()["code_blame"]
    # no get_backend patch needed — validation happens before backend access
    out = tool(commit="abc1234567", repo="")
    assert out.startswith("Error") and "repo" in out


def test_missing_target_errors():
    tool = _registered()["code_blame"]
    out = tool(repo="/r")
    assert out.startswith("Error") and ("commit" in out or "file" in out)
