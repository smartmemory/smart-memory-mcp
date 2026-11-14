"""CLAIMS-21-FIX1: MCP search results name the lanes that found them (``matched_by``), in the item and the tool text."""

from unittest.mock import patch

import pytest

from smartmemory_mcp.backends.models import normalize_item
from smartmemory_mcp.tools.memory_tools import model_item
from tests._tools import tool_fn


class _CoreHit:
    """A core search result: ``to_dict`` leaves the response-only ``_matched_by`` stamp out."""

    def __init__(self, lanes):
        self._matched_by = lanes

    def to_dict(self):
        return {"item_id": "test_mcp_mb_obj", "content": "atlas budget", "memory_type": "semantic"}


class _Backend:
    def __init__(self, items):
        self._items = items

    def search(self, query, top_k=5, **kwargs):
        return self._items


def test_service_rows_and_core_objects_keep_the_lanes():
    row = {"item_id": "test_mcp_mb_row", "content": "a", "score": 0.9, "matched_by": ["lexical", "semantic"]}
    assert normalize_item(row)["matched_by"] == ["lexical", "semantic"]
    assert normalize_item(_CoreHit(["semantic"]))["matched_by"] == ["semantic"]
    assert model_item(normalize_item(row))["matched_by"] == ["lexical", "semantic"]


def test_an_older_server_or_unrecorded_path_reads_as_none():
    assert normalize_item({"item_id": "test_mcp_mb_old", "content": "a"})["matched_by"] is None
    assert normalize_item(_CoreHit(None))["matched_by"] is None


@pytest.mark.parametrize("catalog_mode", [False, True], ids=["full", "catalog"])
def test_search_text_names_the_lanes(catalog_mode):
    items = [
        normalize_item({"item_id": "test_mcp_mb_a", "content": "a", "score": 0.9, "matched_by": ["lexical", "semantic"]}),
        normalize_item({"item_id": "test_mcp_mb_b", "content": "b", "score": 0.5}),
    ]
    with patch("smartmemory_mcp.tools.common._backend", _Backend(items)):
        text = tool_fn("memory_search")(query="atlas budget", catalog_mode=catalog_mode)
    assert text.count("found by: lexical, semantic") == 1
    assert text.count("found by:") == 1
