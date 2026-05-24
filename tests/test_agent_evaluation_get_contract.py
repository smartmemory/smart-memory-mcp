"""Standalone MCP `agent_evaluation_get` return-type contract — CORE-AGENT-2 S03 coverage.

The MCP tool must return `dict | None`, NOT a formatted string (the prior
recall-profile-style return). Codex Round-3 cross-slice fix.

This is the gap that the post-ship review caught: every other agent_*
MCP tool in this file returns a human-readable string, so the easy path
is to copy that pattern. Lock the contract via a forcing-function test.
"""

from __future__ import annotations

from typing import Optional
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

    def test_cold_start_returns_none(self):
        """When the underlying get_evaluation returns None, the MCP tool
        must propagate None (NOT 'no evaluation found' string).
        """
        fn = self._register()

        with patch("smartmemory_mcp.tools.agent_tools.get_backend") as gb, \
             patch("smartmemory.agents.evaluation.get_evaluation") as ge:
            gb.return_value = object()  # backend stand-in
            ge.return_value = None

            result = fn(
                agent_id="alpha",
                dimension="decision_volume",
                domain="python",
            )

        assert result is None, (
            f"agent_evaluation_get must return None on cold-start, got {result!r} "
            f"(type {type(result).__name__})"
        )
        assert not isinstance(result, str), (
            "agent_evaluation_get returned a string on cold-start — contract violation"
        )

    def test_hot_path_returns_dict(self):
        """When the underlying get_evaluation returns a dict, MCP tool
        propagates it as a dict (NOT a json.dumps string).
        """
        fn = self._register()
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
        with patch("smartmemory_mcp.tools.agent_tools.get_backend") as gb, \
             patch("smartmemory.agents.evaluation.get_evaluation") as ge:
            gb.return_value = object()
            ge.return_value = canned

            result = fn(
                agent_id="alpha",
                dimension="decision_volume",
                domain="python",
            )

        assert isinstance(result, dict), (
            f"agent_evaluation_get must return dict, got {type(result).__name__}: {result!r}"
        )
        # Verify shape passes through unchanged (no stringification)
        assert result["dimension"] == "decision_volume"
        assert result["score"] == 0.75
