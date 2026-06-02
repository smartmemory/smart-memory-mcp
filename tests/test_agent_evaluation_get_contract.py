"""Standalone MCP `agent_evaluation_get` return-type contract — CORE-AGENT-2 S03 coverage.

The MCP tool must return `dict | None`, NOT a formatted string (the prior
recall-profile-style return). Codex Round-3 cross-slice fix.

This is the gap that the post-ship review caught: every other agent_*
MCP tool in this file returns a human-readable string, so the easy path
is to copy that pattern. Lock the contract via a forcing-function test.
"""

from __future__ import annotations

from unittest.mock import patch


class _FakeMCP:
    """Capture tool functions registered via @mcp.tool() decorator."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class TestAgentEvaluationGetReturnType:
    """`agent_evaluation_get` must return dict | None, never a string."""

    def _register(self):
        from smartmemory_mcp.tools import agent_tools

        mcp = _FakeMCP()
        agent_tools.register(mcp)
        assert "agent_evaluation_get" in mcp.tools, (
            "agent_evaluation_get not registered by smart_memory_mcp.tools.agent_tools.register()"
        )
        return mcp.tools["agent_evaluation_get"]

    def test_signature_returns_optional_dict(self):
        """Annotated return type must be Optional[Dict] / dict | None — NOT str."""
        import typing

        fn = self._register()
        hints = typing.get_type_hints(fn)
        ret = hints.get("return")
        assert ret is not None, "agent_evaluation_get has no return annotation"
        # Accept Optional[Dict], Optional[dict], dict | None, Dict | None
        ret_str = str(ret).lower()
        assert "dict" in ret_str, (
            f"agent_evaluation_get return annotation does not mention dict; "
            f"got {ret!r} ({ret_str!r})"
        )
        assert "str" not in ret_str or "dict" in ret_str, (
            f"agent_evaluation_get return annotation includes str; "
            f"the contract requires dict | None (not a formatted string). "
            f"got {ret!r}"
        )

    @staticmethod
    def _local_backend():
        """A local-mode backend stand-in: exposes `_mem` (the real SmartMemory
        in production). The 2026-06-02 bug hunt found the tool passed the MCP
        backend WRAPPER to get_evaluation (which has no graph) so it ALWAYS
        returned None; the fix passes `backend._mem`."""

        class _LocalBackend:
            _mem = object()  # stands in for the real SmartMemory

        return _LocalBackend()

    def test_cold_start_returns_none(self):
        """Local backend present, get_evaluation returns None (cold-start) ->
        the tool propagates None (NOT a 'no evaluation found' string), and it
        passed the SmartMemory (backend._mem), not the wrapper."""
        fn = self._register()
        backend = self._local_backend()

        with (
            patch("smartmemory_mcp.tools.agent_tools.get_backend") as gb,
            patch("smartmemory.agents.evaluation.get_evaluation") as ge,
        ):
            gb.return_value = backend
            ge.return_value = None

            result = fn(agent_id="alpha", dimension="decision_volume", domain="python")

        assert result is None
        assert not isinstance(result, str)
        assert ge.call_args[0][0] is backend._mem, (
            "must pass backend._mem (SmartMemory), not the wrapper"
        )

    def test_hot_path_returns_dict(self):
        """get_evaluation returns a dict -> propagated as a dict, and it received
        backend._mem (the SmartMemory), not the MCP wrapper (the always-None bug)."""
        fn = self._register()
        backend = self._local_backend()
        canned = {
            "evaluation_id": "alpha/decision_volume/python/2026-05-24",
            "agent_id": "alpha",
            "dimension": "decision_volume",
            "domain": "python",
            "score": 0.75,
            "score_delta": 0.05,
            "trend": "improving",
            "basis": [],
            "sample_size": 0,
            "valid_from": "2026-05-24T00:00:00+00:00",
            "recorded_at": "2026-05-24T00:00:00+00:00",
            "evaluator": "EvaluationEvolver/v1",
        }
        with (
            patch("smartmemory_mcp.tools.agent_tools.get_backend") as gb,
            patch("smartmemory.agents.evaluation.get_evaluation") as ge,
        ):
            gb.return_value = backend
            ge.return_value = canned

            result = fn(agent_id="alpha", dimension="decision_volume", domain="python")

        assert isinstance(result, dict), (
            f"agent_evaluation_get must return dict, got {type(result).__name__}: {result!r}"
        )
        assert result["dimension"] == "decision_volume"
        assert result["score"] == 0.75
        # Forcing function for the bug: the SmartMemory (backend._mem) is passed.
        assert ge.call_args[0][0] is backend._mem, (
            "must pass backend._mem (SmartMemory), not the wrapper"
        )

    def test_remote_mode_returns_none_without_calling_get_evaluation(self):
        """Remote backend has no `_mem` -> tool returns None and never calls
        get_evaluation client-side (remote eval read is a follow-on)."""
        fn = self._register()
        with (
            patch("smartmemory_mcp.tools.agent_tools.get_backend") as gb,
            patch("smartmemory.agents.evaluation.get_evaluation") as ge,
        ):
            gb.return_value = object()  # remote-mode stand-in: no _mem
            result = fn(agent_id="alpha", dimension="decision_volume", domain="python")
        assert result is None
        ge.assert_not_called()
