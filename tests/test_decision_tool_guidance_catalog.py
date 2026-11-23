"""BUG-MCP-DECISION-TOOL-GUIDANCE-1: the advertised stdio catalog tells an assistant what it needs (notes G08-G19).

Asserts the real server catalog (list_tools converted to the MCP wire form), not the Python docstrings.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MCP_ROOT = Path(__file__).resolve().parent.parent

_SNIPPET = """
import asyncio, json
import smartmemory_mcp.capabilities as capabilities
import smartmemory_mcp.tier as tier
from smartmemory_mcp.backends.remote import RemoteBackend
tier.get_api_key = lambda: "sk_test_key_123"
capabilities.get_backend = lambda: RemoteBackend(api_key="sk_test_key_123")
from smartmemory_mcp.server import mcp
wire = [t.to_mcp_tool() for t in asyncio.run(mcp.list_tools())]
print(json.dumps({t.name: {"d": t.description or "", "p": sorted((t.inputSchema or {}).get("properties", {})),
                           "r": sorted((t.inputSchema or {}).get("required", []))} for t in wire}))
"""


@pytest.fixture(scope="module")
def catalog():
    env = {k: v for k, v in os.environ.items() if k not in ("SMARTMEMORY_API_KEY", "SMARTMEMORY_MCP_FULL_TOOLS")}
    env.update(SMARTMEMORY_API_KEY="sk_test_key_123", SMARTMEMORY_MCP_FULL_TOOLS="true")
    run = subprocess.run([sys.executable, "-c", _SNIPPET], capture_output=True, text=True, env=env, cwd=MCP_ROOT)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def _d(catalog, name):
    return catalog[name]["d"]


def test_g08_reinforce_says_where_the_evidence_id_comes_from(catalog):
    d = _d(catalog, "decision_reinforce")
    assert "memory_save" in d and "Item ID" in d and "memory_search" in d
    assert "not the decision id" in d and "decision_link_evidence" in d


def test_g09_contradict_says_where_the_evidence_id_comes_from(catalog):
    d = _d(catalog, "decision_contradict")
    assert "memory_save" in d and "Item ID" in d and "not the decision id" in d
    assert "stays current" in d


def test_g17_create_pending_names_requirement_types_and_default(catalog):
    d = _d(catalog, "decision_create_pending")
    for value in ("``evidence``", "``confirmation``", "``data``"):
        assert value in d
    assert "when left out" in d and '"requirement_type": "evidence"' in d
    assert "req_<uuid8>" in d  # existing guidance kept


def test_g18_activate_vs_inspect(catalog):
    d = _d(catalog, "decision_try_activate")
    assert "changes the decision's state" in d and "decision_get" in d and "nothing changes" in d
    g = _d(catalog, "decision_get")
    assert "only reads" in g and "still open" in g
    assert "as_of" in g  # existing guidance kept


def test_g19_resolve_requirement_sources_and_next_step(catalog):
    d = _d(catalog, "decision_resolve_requirement")
    assert "req_" in d and "decision_create_pending" in d
    assert "memory_save" in d and "Item ID" in d
    assert "does not activate" in d and "decision_try_activate" in d


@pytest.mark.parametrize(
    "name,props,required",
    [
        ("decision_reinforce", ["decision_id", "evidence_id"], ["decision_id", "evidence_id"]),
        ("decision_contradict", ["decision_id", "evidence_id"], ["decision_id", "evidence_id"]),
        ("decision_create_pending", ["content", "domain", "requirements", "tags"], ["content", "requirements"]),
        ("decision_try_activate", ["decision_id"], ["decision_id"]),
        ("decision_resolve_requirement", ["decision_id", "memory_id", "requirement_id"],
         ["decision_id", "memory_id", "requirement_id"]),
        ("decision_get", ["as_of", "business_scope", "decision_id", "valid_at"], ["decision_id"]),
    ],
)
def test_schema_unchanged(catalog, name, props, required):
    assert catalog[name]["p"] == props and catalog[name]["r"] == required


def test_catalog_size_unchanged(catalog):
    assert len(catalog) == 122
