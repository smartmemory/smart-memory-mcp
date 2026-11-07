"""M1: parallel session switches cannot move a composite procedure's authority."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from smartmemory.models.memory_item import MemoryItem
from smartmemory_mcp.tools import common
from . import test_procedure_remote_stores as stores
from .test_procedure_tools import call, create

remote_tools = stores.remote_tools


@pytest.mark.parametrize("operation", ["outcome", "promote", "dismiss"])
def test_operation_pins_workspace_after_parent_read(
    remote_tools, monkeypatch, operation
):
    mcp, (a, b), facades = remote_tools
    wa, wb = a.active_workspace_id, b.active_workspace_id
    monkeypatch.setattr(common, "_backend", a)
    proc = create(mcp, candidate=operation == "promote")
    pid = proc["procedure_id"]
    if operation == "outcome":
        # Force a frozen cursor continuation after the switch, as well as readback.
        for i in range(100):
            facades[wa].add(
                MemoryItem(
                    content=f"test_sm_procedure_pin_{pid}_{i}",
                    memory_type="episodic",
                    metadata={
                        "procedure_outcome_for": pid,
                        "procedure_outcome_schema": 1,
                        "success": True,
                        "note": "seed",
                        "recorded_at": "2026-11-06T10:00:00+00:00",
                    },
                ),
                _embed=False,
            )
    reached, resume = Event(), Event()
    transport = httpx.request
    requests = []

    def paused(method, url, **kwargs):
        response = transport(method, url, **kwargs)
        requests.append(
            (
                method,
                str(url),
                kwargs["headers"]["X-Workspace-Id"],
                kwargs.get("params", {}),
            )
        )
        if (
            method == "GET"
            and str(url).endswith(f"/memory/{pid}")
            and not reached.is_set()
        ):
            assert response.status_code == 200
            reached.set()
            assert resume.wait(20), "switch never released the parent read"
        return response

    monkeypatch.setattr(httpx, "request", paused)
    tool = (
        "procedure_record_outcome"
        if operation == "outcome"
        else f"procedure_{operation}"
    )
    args = (
        {"success": False, "note": "A-private outcome"}
        if operation == "outcome"
        else {}
    )
    result, error = None, None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call, mcp, tool, procedure_id=pid, **args)
        try:
            assert reached.wait(20), "operation never read A's parent"
            # This is the same session mutation committed by server._switch_workspace.
            a.set_active_workspace(wb)
        finally:
            resume.set()
        try:
            result = future.result(timeout=30)
        except Exception as exc:
            error = exc

    # Always inspect BOTH real stores before asserting the operation's result.
    a_rows = facades[wa].list_memories(
        limit=200, metadata_filters={"procedure_outcome_for": pid}
    )
    b_rows = facades[wb].list_memories(
        limit=200, metadata_filters={"procedure_outcome_for": pid}
    )
    a_parent, b_parent = facades[wa].get(pid), facades[wb].get(pid)
    assert b_rows["items"] == [], f"A content leaked to B despite call error={error!r}"
    assert b_parent is None
    assert error is None, repr(error)
    assert a.active_workspace_id == wb  # Pinning does not undo the session switch.
    assert requests and all(row[2] == wa for row in requests)
    if operation == "outcome":
        assert len(a_rows["items"]) == 101
        assert result["success_count"] == 100 and result["failure_count"] == 1
        assert any(row[3].get("cursor") for row in requests)
        assert any(row[0] == "POST" for row in requests)
    else:
        expected = "active" if operation == "promote" else "dismissed"
        assert a_parent.metadata["procedure_status"] == expected
        assert result["status"] == expected
        assert any(row[0] == "PATCH" for row in requests)


def test_unresolved_workspace_refuses_before_any_request(remote_tools, monkeypatch):
    mcp, (a, _), _ = remote_tools
    a.set_active_workspace("")
    monkeypatch.setattr(common, "_backend", a)
    from fastmcp.exceptions import ToolError

    requests = []
    transport = httpx.request

    def observed(*args, **kwargs):
        requests.append(args)
        return transport(*args, **kwargs)

    monkeypatch.setattr(httpx, "request", observed)
    with pytest.raises(ToolError, match="no active workspace"):
        call(mcp, "procedure_list")
    assert requests == []
