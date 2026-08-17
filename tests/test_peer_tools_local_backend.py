"""CORE-ZERO-SCHEMA-1 Phase 1 — the standalone MCP `peer_chat` tool.

Sibling to ``test_code_blame_tool.py``: invokes the registered tool via a capturing
fake MCP (no FastMCP server) and asserts registration, the local-backend passthrough,
the remote-path parked message, and argument validation. The synthesis itself is
covered in smart-memory-core.

Fake backends are explicit classes (not MagicMock) so ``hasattr(backend, "request")``
is controlled — a MagicMock auto-creates ``request`` and would always take the remote
branch.
"""

from __future__ import annotations

from unittest.mock import patch

from smartmemory_mcp.tools import peer_tools


def _registered() -> dict:
    captured: dict = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def _decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorator

    peer_tools.register(_FakeMCP())
    return captured


class _LocalBackend:
    """A local backend: has peer_chat, NO `request` attribute."""

    def __init__(self, result=None):
        self._result = result
        self.calls: list[dict] = []

    def peer_chat(self, **kw):
        self.calls.append(kw)
        return self._result


class _RemoteBackend:
    """A remote backend: has `request` (the remote-path discriminator)."""

    def request(self, *a, **kw):  # pragma: no cover - never called in Phase 1
        return {}


def _result(**ov):
    base = {
        "answer": "Ada prefers dependency injection.",
        "peer_id": "ada",
        "reasoning_level": "medium",
        "model": "test-model",
        "sources": [
            {
                "item_id": "itm-1",
                "memory_type": "episodic",
                "content_preview": "I prefer dependency injection.",
                "channel": "episodic",
            }
        ],
    }
    base.update(ov)
    return base


def test_peer_chat_is_registered():
    assert "peer_chat" in _registered()


def test_peer_chat_local_backend_returns_the_result_verbatim():
    backend = _LocalBackend(_result())
    with patch.object(peer_tools, "get_backend", return_value=backend):
        out = _registered()["peer_chat"](peer_id="ada", query="What about DI?")

    assert out == _result()
    assert backend.calls == [
        {
            "peer_id": "ada",
            "query": "What about DI?",
            "reasoning_level": "medium",
            "session_id": None,
            "top_k": 10,
        }
    ]


def test_peer_chat_forwards_optional_arguments():
    backend = _LocalBackend(_result(reasoning_level="high"))
    with patch.object(peer_tools, "get_backend", return_value=backend):
        _registered()["peer_chat"](
            peer_id="ada",
            query="What about DI?",
            reasoning_level="high",
            session_id="sess-1",
            top_k=3,
        )

    assert backend.calls[0]["reasoning_level"] == "high"
    assert backend.calls[0]["session_id"] == "sess-1"
    assert backend.calls[0]["top_k"] == 3


def test_peer_chat_on_remote_backend_is_parked():
    with patch.object(peer_tools, "get_backend", return_value=_RemoteBackend()):
        out = _registered()["peer_chat"](peer_id="ada", query="What about DI?")

    assert "error" in out
    assert "local capability" in out["error"]


def test_peer_chat_requires_peer_id_and_query():
    backend = _LocalBackend(_result())
    with patch.object(peer_tools, "get_backend", return_value=backend):
        registered = _registered()["peer_chat"]
        assert "error" in registered(peer_id="", query="What about DI?")
        assert "error" in registered(peer_id="ada", query="")

    assert backend.calls == []


def test_peer_chat_rejects_an_unknown_reasoning_level():
    backend = _LocalBackend(_result())
    with patch.object(peer_tools, "get_backend", return_value=backend):
        out = _registered()["peer_chat"](
            peer_id="ada", query="q", reasoning_level="turbo"
        )

    assert "error" in out
    assert backend.calls == []
