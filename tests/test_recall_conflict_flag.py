"""Canonical decoding, model presentation and tool refusal preserve conflict evidence."""

import httpx
import pytest
from fastmcp.exceptions import ToolError
from smartmemory.errors import RecallConflictError
from smartmemory.models.memory_item import MemoryItem
from smartmemory.search import annotate_conflicts
from smartmemory_mcp.backends.models import normalize_item
from smartmemory_mcp.backends.remote import _raise_recall_conflict
from smartmemory_mcp.tools.common import graceful, resolution_text
from smartmemory_mcp.tools.memory_tools import model_item, _format_catalog


def test_supported_conflict_survives_normalization_and_all_presentations():
    items = [
        MemoryItem(
            item_id=f"test_rcf_{n}",
            content=f"Acme permits {n} retries.",
            memory_type="semantic",
            metadata={"tenant_id": "T", "workspace_id": "W"},
        )
        for n in [3, 5]
    ]
    items, evidence = annotate_conflicts(items)
    assert evidence
    normalized = [normalize_item(i) for i in items]
    assert all(i["conflict"] for i in normalized)
    assert model_item(normalized[0])["conflict"] == normalized[0]["conflict"]
    assert "conflict: " in resolution_text(normalized[0])
    assert "conflict: " in _format_catalog("Acme", normalized)


def test_local_and_remote_strict_errors_are_tool_errors():
    @graceful
    def local():
        raise RecallConflictError([("test_rcf_a", "test_rcf_b")])

    with pytest.raises(ToolError, match="recall_conflict"):
        local()
    response = httpx.Response(
        409,
        json={
            "detail": {
                "error": "recall_conflict",
                "message": "Recall contains 1 conflicting pair(s)",
                "pairs": [["test_rcf_a", "test_rcf_b"]],
            }
        },
    )
    with pytest.raises(ToolError, match="recall_conflict"):
        _raise_recall_conflict(response)
    _raise_recall_conflict(httpx.Response(409, json={"detail": "unrelated conflict"}))
