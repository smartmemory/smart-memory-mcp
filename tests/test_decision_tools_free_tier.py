"""DEV-ONBOARD-FU-MCP-DECISION-TOOLS-FREE-1: decision tools are FREE in the keyless server.

A keyless local session (no API key, no `SMARTMEMORY_MCP_FULL_TOOLS`) must list and
accept calls to every decision tool, matching the Python SDK where `add_decision`
needs no key. Only the decision suite moved: tools that were PRO stay PRO.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.test_tier_registration import MCP_ROOT, _clean_env, _run_snippet

# (tool, arguments) — one row per decision tool registered by `decision_tools.register`.
# The completeness test below fails when a tool is added there without a row here.
DECISION_ROWS = (
    ("decision_create", {"content": "Job queue runs on Postgres"}),
    ("decision_get", {"decision_id": "dec_1"}),
    ("decision_list", {}),
    ("decision_search", {"topic": "job queue"}),
    (
        "decision_supersede",
        {"decision_id": "dec_1", "new_content": "Use SQS", "reason": "scale"},
    ),
    ("decision_retract", {"decision_id": "dec_1", "reason": "obsolete"}),
    ("decision_reinforce", {"decision_id": "dec_1", "evidence_id": "mem_1"}),
    ("decision_contradict", {"decision_id": "dec_1", "evidence_id": "mem_1"}),
    ("decision_provenance", {"decision_id": "dec_1"}),
    ("decision_find_conflicts", {"decision_id": "dec_1"}),
    (
        "decision_create_pending",
        {
            "content": "Ship the ledger",
            "requirements": [
                {"description": "Load test", "requirement_type": "evidence"}
            ],
        },
    ),
    (
        "decision_resolve_requirement",
        {"decision_id": "dec_1", "requirement_id": "req_1", "memory_id": "mem_1"},
    ),
    ("decision_try_activate", {"decision_id": "dec_1"}),
)
DECISION_TOOLS = tuple(name for name, _ in DECISION_ROWS)

# Tools outside the decision suite that were PRO before this change and stay PRO.
STILL_PRO = (
    "memory_add",
    "code_search",
    "memory_anchor_set",
    "agent_get_recall_profile",
)

# Keyless server, recording backend: each decision tool is called through the real
# FastMCP dispatch and the snippet reports whether the call reached `backend.decision_*`.
_CALL_SNIPPET = """\
import asyncio
import json
import sys
import smartmemory_mcp.tier as tier
import smartmemory_mcp.capabilities as capabilities
import smartmemory_mcp.tools.decision_tools as decision_tools

DECISION = {"decision_id": "dec_1", "content": "x", "decision_type": "choice",
            "confidence": 0.7, "status": "active", "ignorance": 1.0}
reached = []


class Recorder:
    def __getattr__(self, name):
        def call(*args, **kwargs):
            reached.append(name)
            if name in ("decision_list", "decision_search"):
                return [DECISION]
            if name == "decision_find_conflicts":
                return {"conflicts": [DECISION]}
            if name in ("decision_resolve_requirement",):
                return True
            return DECISION
        return call

    def supports(self, capability):
        return True


tier.get_api_key = lambda: ""
capabilities.get_backend = lambda: Recorder()
decision_tools.get_backend = lambda: Recorder()
from smartmemory_mcp.server import mcp

rows = json.loads(sys.argv[1])
out = {}
for name, args in rows:
    reached.clear()
    try:
        asyncio.run(mcp.call_tool(name, args))
        err = None
    except Exception as exc:  # rendering errors still prove the tool was callable
        err = type(exc).__name__ + ": " + str(exc)
    out[name] = {"reached": name in reached, "error": err}
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def keyless_tools():
    return set(_run_snippet(_clean_env())["names"])


@pytest.fixture(scope="module")
def pro_tools():
    return set(_run_snippet(_clean_env(SMARTMEMORY_API_KEY="sk_test_key_123"))["names"])


@pytest.fixture(scope="module")
def keyless_calls():
    result = subprocess.run(
        [sys.executable, "-c", _CALL_SNIPPET, json.dumps(DECISION_ROWS)],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
        cwd=MCP_ROOT,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("name", DECISION_TOOLS)
def test_decision_tool_listed_keyless(keyless_tools, name):
    assert name in keyless_tools


@pytest.mark.parametrize("name", DECISION_TOOLS)
def test_decision_tool_callable_keyless(keyless_calls, name):
    row = keyless_calls[name]
    assert row["reached"], f"{name} never reached its backend method: {row['error']}"
    assert row["error"] is None, f"{name} raised keyless: {row['error']}"


def test_decision_rows_cover_the_suite():
    from smartmemory_mcp.tools import decision_tools

    class _Collect:
        def __init__(self):
            self.names = set()

        def tool(self, *args, **kwargs):
            def deco(fn):
                self.names.add(fn.__name__)
                return fn

            return deco

    collected = _Collect()
    decision_tools.register(collected)
    assert collected.names == set(DECISION_TOOLS)


@pytest.mark.parametrize("name", STILL_PRO)
def test_non_decision_pro_tool_stays_pro(keyless_tools, pro_tools, name):
    assert name in pro_tools
    assert name not in keyless_tools


def test_keyed_session_keeps_decision_tools(pro_tools):
    assert set(DECISION_TOOLS) <= pro_tools


def test_env_is_hermetic():
    # The helper strips the key; guard against a future helper change leaking one in.
    assert "SMARTMEMORY_API_KEY" not in _clean_env()
    assert os.path.isdir(MCP_ROOT)
