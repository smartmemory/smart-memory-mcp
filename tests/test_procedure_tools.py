"""SM-PROCEDURE-MCP-1: six tools exercised on real disposable Lite storage."""

import asyncio

from fastmcp import FastMCP
import pytest

from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import common, procedure_tools


@pytest.fixture
def tools(tmp_path, monkeypatch):
    memory = create_lite_memory(
        data_dir=str(tmp_path / "test_sm_procedure_mcp"),
        pipeline_profile=PipelineConfig.lite(llm_enabled=False),
        spawn_worker=False,
    )
    backend = LocalBackend()
    backend._mem = memory
    monkeypatch.setattr(common, "_backend", backend)
    mcp = FastMCP("procedure-test")
    procedure_tools.register(mcp)
    try:
        yield mcp, memory
    finally:
        memory.close()


def call(mcp, tool, **args):
    result = asyncio.run(mcp.call_tool(tool, args))
    data = result.structured_content
    return (
        data["result"] if isinstance(data, dict) and set(data) == {"result"} else data
    )


def create(mcp, **kwargs):
    return call(
        mcp,
        "procedure_create",
        name="Recover database",
        steps=["Check backup", "Restore database"],
        preconditions=["Backup exists"],
        expected_outcome="Database healthy",
        **kwargs,
    )


def test_all_six_tools_persist_effects(tools):
    mcp, memory = tools
    proc = create(mcp, candidate=True)
    pid = proc["procedure_id"]
    assert call(mcp, "procedure_list") == []
    assert call(mcp, "procedure_promote", procedure_id=pid)["status"] == "active"
    assert call(mcp, "procedure_get", procedure_id=pid)["steps"] == [
        "Check backup",
        "Restore database",
    ]
    assert (
        call(mcp, "procedure_list", task="restore database")[0]["procedure_id"] == pid
    )
    good = call(
        mcp, "procedure_record_outcome", procedure_id=pid, success=True, note="healthy"
    )
    bad = call(
        mcp, "procedure_record_outcome", procedure_id=pid, success=False, note="failed"
    )
    assert bad["confidence"] < good["confidence"]
    assert memory.get(pid).memory_type == "procedural"
    assert memory.get(bad["last_outcomes"][0]["outcome_id"]).memory_type == "episodic"
    assert (
        call(mcp, "procedure_dismiss", procedure_id=pid, reason="obsolete")["status"]
        == "dismissed"
    )
    assert call(mcp, "procedure_list") == []


def test_tools_have_no_scope_override(tools):
    mcp, _ = tools
    listed = asyncio.run(mcp.list_tools())
    assert {t.name for t in listed} == {
        "procedure_create",
        "procedure_list",
        "procedure_get",
        "procedure_promote",
        "procedure_dismiss",
        "procedure_record_outcome",
    }
    for tool in listed:
        assert not (
            {"workspace_id", "tenant_id", "user_id", "team_id", "context"}
            & tool.parameters["properties"].keys()
        )
    assert call(mcp, "procedure_get", procedure_id="test_sm_procedure_missing") is None


def test_invalid_and_unknown_writes_are_errors(tools):
    from fastmcp.exceptions import ToolError

    mcp, _ = tools
    with pytest.raises(ToolError):
        call(mcp, "procedure_create", name="x", steps=[], expected_outcome="healthy")
    with pytest.raises(ToolError):
        call(
            mcp,
            "procedure_record_outcome",
            procedure_id="test_sm_procedure_missing",
            success=True,
        )
