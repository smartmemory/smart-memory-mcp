"""Integration tests for tier resolution and tool registration counts.

Tests Tasks 26 from PLAT-MCP-UNIFY-1 plan:
- resolve_tier returns correct tier based on env vars
- Server registers correct number of tools per tier

Tool registration happens at module import time (_register_tools()), so we use
subprocess to get a clean Python process for each tier test.
"""

import json
import os
import subprocess
import sys


from smartmemory_mcp.tier import Tier, resolve_tier

MCP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Snippet executed in subprocess to count tools and list names
_COUNT_SNIPPET = """\
import asyncio
import json
from smartmemory_mcp.server import mcp
tools = asyncio.run(mcp.list_tools())
print(json.dumps({"count": len(tools), "names": sorted(tool.name for tool in tools)}))
"""


def _clean_env(**overrides) -> dict[str, str]:
    """Return a copy of os.environ with tier-related vars removed, then overrides applied."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SMARTMEMORY_API_KEY", "SMARTMEMORY_MCP_FULL_TOOLS")
    }
    env.update(overrides)
    return env


def _run_snippet(env: dict[str, str]) -> dict:
    """Run the count snippet in a subprocess and return parsed JSON."""
    result = subprocess.run(
        [sys.executable, "-c", _COUNT_SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        cwd=MCP_ROOT,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Parse the last line (skip any warnings on stderr)
    output = result.stdout.strip().splitlines()[-1]
    return json.loads(output)


# ---------------------------------------------------------------------------
# Tier resolution tests (in-process, safe because resolve_tier is stateless)
# ---------------------------------------------------------------------------


class TestResolveTier:
    def test_resolve_tier_free(self, monkeypatch):
        """No API key anywhere -> FREE."""
        monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
        monkeypatch.delenv("SMARTMEMORY_MCP_FULL_TOOLS", raising=False)
        # Also block keyring / file fallback paths
        monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "")
        assert resolve_tier() == Tier.FREE

    def test_resolve_tier_pro_from_env(self, monkeypatch):
        """SMARTMEMORY_API_KEY env var set -> PRO."""
        monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test_key_123")
        monkeypatch.delenv("SMARTMEMORY_MCP_FULL_TOOLS", raising=False)
        assert resolve_tier() == Tier.PRO

    def test_resolve_tier_pro_plus(self, monkeypatch):
        """API key + SMARTMEMORY_MCP_FULL_TOOLS=true -> PRO_PLUS."""
        monkeypatch.setenv("SMARTMEMORY_API_KEY", "sk_test_key_123")
        monkeypatch.setenv("SMARTMEMORY_MCP_FULL_TOOLS", "true")
        assert resolve_tier() == Tier.PRO_PLUS

    def test_resolve_tier_pro_plus_requires_pro(self, monkeypatch):
        """SMARTMEMORY_MCP_FULL_TOOLS=true but NO API key -> FREE (not PRO+)."""
        monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
        monkeypatch.delenv("SMARTMEMORY_MCP_FULL_TOOLS", raising=False)
        monkeypatch.setattr("smartmemory_mcp.tier.get_api_key", lambda: "")
        # Even with full tools flag, no key means FREE
        monkeypatch.setenv("SMARTMEMORY_MCP_FULL_TOOLS", "true")
        assert resolve_tier() == Tier.FREE


# ---------------------------------------------------------------------------
# Tool count tests (subprocess — clean Python process per tier)
# ---------------------------------------------------------------------------

FREE_TOOLS = sorted(
    [
        "login",
        "whoami",
        "switch_team",
        "memory_ingest",
        "memory_search",
        "memory_recall",
        "memory_get",
        "memory_explain",  # PLAT-AUDITABLE-MEMORY-1 — FREE by design (positioning wedge)
        "memory_export",
        "memory_import",
        "memory_migrate",
        "memory_auto",  # DIST-AGENT-HOOKS-1
        "memory_feedback",  # SELF-IMPROVE-* feedback surface
        "get_working_context",  # CORE-MEMORY-DYNAMICS-1 M1a
        "read_around",  # CORE-RECALL-CENTERED-1 Phase 2
        "memory_recall_pack",  # CORE-RECALL-BUDGET-1 — budgeted context assembly, FREE by design
        "memory_policy_bundle",  # GOV-STRATUM-SEAM-1 P1 — required before a runner plans
    ]
)


class TestToolRegistration:
    def test_free_tier_tool_count(self):
        """FREE tier registers exactly 17 tools.

        NOTE: `_clean_env` only strips the two tier env vars — it cannot reach the
        keyring/file credential store that `tier.get_api_key()` also consults. On a
        developer machine that is logged in (smartmemory_app keyring), this resolves
        to a paid tier and the count comes back 69 instead of 16. That is a local
        artifact, NOT a regression; CI and any clean container see FREE correctly.
        Verify locally with: docker run --rm -v "$PWD":/w -w /w python:3.11-slim \
        sh -c "pip install -q -e . pytest && pytest tests/ -q"
        """
        env = _clean_env()
        data = _run_snippet(env)
        assert data["count"] == 17, (
            f"Expected 17 FREE tools, got {data['count']}: {data['names']}"
        )
        assert data["names"] == FREE_TOOLS

    def test_pro_tier_tool_count(self):
        """PRO tier registers exactly 70 tools (+transcript_search/_status, DIST-CC-INGEST-1 Phase 4)."""
        env = _clean_env(SMARTMEMORY_API_KEY="sk_test_key_123")
        data = _run_snippet(env)
        assert data["count"] == 70, (
            f"Expected 70 PRO tools, got {data['count']}: {data['names']}"
        )
        # Verify FREE tools are a subset of PRO tools
        for tool in FREE_TOOLS:
            assert tool in data["names"], f"FREE tool {tool!r} missing from PRO tier"

    def test_pro_plus_tier_tool_count(self):
        """PRO_PLUS tier registers exactly 101 tools (+transcript_search/_status, DIST-CC-INGEST-1 Phase 4)."""
        env = _clean_env(
            SMARTMEMORY_API_KEY="sk_test_key_123", SMARTMEMORY_MCP_FULL_TOOLS="true"
        )
        data = _run_snippet(env)
        assert data["count"] == 101, (
            f"Expected 101 PRO_PLUS tools, got {data['count']}: {data['names']}"
        )
        # Verify FREE tools are a subset of PRO_PLUS tools
        for tool in FREE_TOOLS:
            assert tool in data["names"], (
                f"FREE tool {tool!r} missing from PRO_PLUS tier"
            )
