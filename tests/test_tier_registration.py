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

import pytest

from smartmemory_mcp.tier import Tier, resolve_tier

MCP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Snippet executed in subprocess to count tools and list names
_COUNT_SNIPPET = """\
import asyncio
import json
import os
import smartmemory_mcp.tier as tier
import smartmemory_mcp.capabilities as capabilities
from smartmemory_mcp.backends.remote import RemoteBackend

# Isolate registration from stored login credentials and local database setup.
# The listing still runs the real capability middleware against a remote backend.
tier.get_api_key = lambda: os.environ.get("SMARTMEMORY_API_KEY", "")
capabilities.get_backend = lambda: RemoteBackend(api_key="sk_test_key_123")
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
        check=False,
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
        "switch_workspace",  # MCP-SWITCH-WORKSPACE-1
        "switch_team",  # deprecated alias of switch_workspace, one release
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
        # DEV-ONBOARD-FU-MCP-DECISION-TOOLS-FREE-1 — the decision suite is FREE
        # (tests/test_decision_tools_free_tier.py pins each name).
        "decision_create",
        "decision_get",
        "decision_list",
        "decision_search",
        "decision_supersede",
        "decision_retract",
        "decision_reinforce",
        "decision_contradict",
        "decision_provenance",
        "decision_find_conflicts",
        "decision_create_pending",
        "decision_resolve_requirement",
        "decision_try_activate",
    ]
)


# tier, env overrides, registered count (MCP-SWITCH-WORKSPACE-1 added switch_workspace
# to every tier; switch_team stays registered as its deprecated alias).
TIER_COUNTS = [
    ("FREE", {}, 31),
    ("PRO", {"SMARTMEMORY_API_KEY": "sk_test_key_123"}, 69),
    (
        "PRO_PLUS",
        {
            "SMARTMEMORY_API_KEY": "sk_test_key_123",
            "SMARTMEMORY_MCP_FULL_TOOLS": "true",
        },
        100,
    ),
]


class TestToolRegistration:
    @pytest.mark.parametrize(("tier", "overrides", "expected"), TIER_COUNTS)
    def test_tier_tool_count(self, tier, overrides, expected):
        """Each tier registers exactly its count; FREE is the exact name list and a
        subset of every paid tier (+transcript_search/_status, DIST-CC-INGEST-1 Phase 4)."""
        data = _run_snippet(_clean_env(**overrides))
        assert data["count"] == expected, (
            f"Expected {expected} {tier} tools, got {data['count']}: {data['names']}"
        )
        if tier == "FREE":
            assert data["names"] == FREE_TOOLS
            return
        assert {"code_blame", "code_read_transcript", "peer_chat"}.isdisjoint(
            data["names"]
        )
        assert "memory_ingest_document" in data["names"]
        missing = sorted(set(FREE_TOOLS) - set(data["names"]))
        assert not missing, f"FREE tools missing from {tier}: {missing}"
