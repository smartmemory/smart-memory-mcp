"""Canonical decoding, model presentation and tool refusal preserve conflict evidence."""

import json

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


def test_scoped_validity_text_still_renders_conflict_and_peer_evidence():
    """D-0039 r1 (M2): a scoped-validity diagnostic no longer hides the conflict warning."""
    from unittest.mock import patch

    from tests._tools import tool_fn

    items = [
        MemoryItem(
            item_id=f"test_rcf_sv_{n}",
            content=f"Acme permits {n} retries.",
            memory_type="semantic",
            metadata={"tenant_id": "T", "workspace_id": "W"},
        )
        for n in [3, 5]
    ]
    items, evidence = annotate_conflicts(items)
    assert evidence
    normalized = [normalize_item(i) for i in items]
    resolution = {"selected_item_id": "test_rcf_sv_3"}
    for item in normalized:
        item["metadata"] = {
            **(item.get("metadata") or {}),
            "scoped_validity_resolution": resolution,
        }
        assert item["conflict"]

    class Backend:
        def search(self, query, top_k=5, **kwargs):
            return normalized

    fn = tool_fn("memory_search")
    for catalog_mode in (False, True):
        with patch("smartmemory_mcp.tools.common._backend", Backend()):
            text = fn(query="Acme retries", catalog_mode=catalog_mode)
        assert text.count("scoped_validity_resolution: ") == 2, text
        assert text.count("conflict: ") == 2, text
        # Each row names its scoped peer.
        peers = [
            json.loads(line.split("conflict: ", 1)[1])["with_item_ids"]
            for line in text.splitlines()
            if line.strip().startswith("conflict: ")
        ]
        assert sorted(peers) == [["test_rcf_sv_3"], ["test_rcf_sv_5"]]


# BUG-FAIL-IF-CONFLICTED-SOURCE-DATE-1: the "could not decide" result reaches an MCP caller as itself.
UNDECIDED_BODY = {
    "detail": {
        "error": "conflict_check_undecided",
        "message": "Recall conflict check could not decide 1 pair(s): r",
        "pairs": [["test_ccu_a", "test_ccu_b"]],
        "reason": "r",
        "method": "jev",
        "hint": "set a key",
        "pairs_not_checked": 3,
    }
}


def test_remote_undecided_409_is_a_tool_error_with_every_field_unchanged():
    with pytest.raises(ToolError) as error:
        _raise_recall_conflict(httpx.Response(409, json=UNDECIDED_BODY))
    text = str(error.value)
    assert text.startswith("conflict_check_undecided: Recall conflict check could not decide 1 pair(s)")
    extra = json.loads(text.split("\n", 1)[1])
    assert extra == {k: v for k, v in UNDECIDED_BODY["detail"].items() if k not in ("error", "message")}


def test_remote_recall_conflict_carries_method():
    body = {"detail": {"error": "recall_conflict", "message": "m", "pairs": [["a", "b"]], "method": "jev"}}
    with pytest.raises(ToolError, match="recall_conflict: m") as error:
        _raise_recall_conflict(httpx.Response(409, json=body))
    assert json.loads(str(error.value).split("\n", 1)[1])["method"] == "jev"


def test_local_undecided_error_is_a_tool_error_with_its_fields():
    from smartmemory.errors import ConflictCheckUndecidedError

    error = ConflictCheckUndecidedError([{"item_ids": ["test_ccu_a", "test_ccu_b"], "reason": "r"}])

    @graceful
    def local():
        raise error

    with pytest.raises(ToolError) as raised:
        local()
    text = str(raised.value)
    assert text.startswith("conflict_check_undecided: ")
    extra = json.loads(text.split("\n", 1)[1])
    assert extra["pairs"] == [["test_ccu_a", "test_ccu_b"]] and extra["reason"] == "r"


def test_search_shows_the_mark_marker_from_the_hosted_service(monkeypatch):
    """The ``mark`` setting: rows come back and the marker is printed unchanged."""
    from unittest.mock import patch

    from smartmemory_mcp.backends.remote import RemoteBackend
    from tests._tools import tool_fn

    marker = {"method": "aligned_text", "undecided_pairs": [{"item_ids": ["test_ccu_a", "test_ccu_b"], "reason": "r"}]}
    rows = [
        {"item_id": "test_ccu_a", "content": "KX-410 costs 1,200 EUR.", "memory_type": "semantic", "conflict": None},
        {"item_id": "test_ccu_b", "content": "KX-410 price is 1,450 EUR.", "memory_type": "semantic", "conflict": None},
    ]
    backend = RemoteBackend(api_url="http://testserver", api_key="k", team_id="t")
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request",
        lambda *a, **k: httpx.Response(200, json={"results": rows, "conflict_check": marker}, request=httpx.Request("POST", "http://testserver")),
    )
    fn = tool_fn("memory_search")
    expected = "conflict_check: " + json.dumps(marker, sort_keys=True)
    for catalog_mode in (False, True):
        with patch("smartmemory_mcp.tools.common._backend", backend):
            assert expected in fn(query="KX-410", fail_if_conflicted=True, catalog_mode=catalog_mode)
    # No marker in the body: nothing printed, and a stale marker from the earlier call is gone.
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request",
        lambda *a, **k: httpx.Response(200, json={"results": rows}, request=httpx.Request("POST", "http://testserver")),
    )
    with patch("smartmemory_mcp.tools.common._backend", backend):
        assert "conflict_check:" not in fn(query="KX-410")


# BUG-CONFLICT-BACKEND-RECALL-FLAG-1: the two backend ``recall`` methods used to take the flag and drop it.

_CONTRADICTING = [
    {"item_id": "test_bcr_a", "content": "KX-410 costs 1,200 EUR.", "memory_type": "semantic", "conflict": None},
    {"item_id": "test_bcr_b", "content": "KX-410 price is 1,450 EUR.", "memory_type": "semantic", "conflict": None},
]
_BCR_MARKER = {"method": "aligned_text", "undecided_pairs": [{"item_ids": ["test_bcr_a", "test_bcr_b"], "reason": "r"}]}


def _remote_with(monkeypatch, response_json, status=200):
    from smartmemory_mcp.backends.remote import RemoteBackend

    sent = []

    def fake(method, url, **kwargs):
        sent.append(kwargs.get("json"))
        return httpx.Response(status, json=response_json, request=httpx.Request("POST", "http://testserver"))

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", fake)
    return RemoteBackend(api_url="http://testserver", api_key="k", team_id="t"), sent


def test_remote_recall_sends_the_flag_and_prints_the_undecided_marker(monkeypatch):
    backend, sent = _remote_with(monkeypatch, {"results": _CONTRADICTING, "conflict_check": _BCR_MARKER})
    text = backend.recall(cwd="KX-410", top_k=5, fail_if_conflicted=True)
    assert sent[0]["fail_if_conflicted"] is True
    assert "KX-410 costs 1,200 EUR." in text and "KX-410 price is 1,450 EUR." in text
    assert text.endswith("conflict_check: " + json.dumps(_BCR_MARKER, sort_keys=True))


def test_remote_recall_empty_rows_still_carry_the_undecided_marker(monkeypatch):
    backend, _ = _remote_with(monkeypatch, {"results": [], "conflict_check": _BCR_MARKER})
    assert backend.recall(cwd="KX-410", fail_if_conflicted=True) == "conflict_check: " + json.dumps(
        _BCR_MARKER, sort_keys=True
    )


def test_remote_recall_proven_conflict_refuses_with_the_service_409(monkeypatch):
    body = {"detail": {"error": "recall_conflict", "message": "m", "pairs": [["test_bcr_a", "test_bcr_b"]]}}
    backend, _ = _remote_with(monkeypatch, body, status=409)
    with pytest.raises(ToolError, match="recall_conflict"):
        backend.recall(cwd="KX-410", fail_if_conflicted=True)


def test_remote_recall_without_the_flag_is_unchanged(monkeypatch):
    backend, sent = _remote_with(monkeypatch, {"results": _CONTRADICTING})
    text = backend.recall(cwd="KX-410", top_k=5)
    assert "fail_if_conflicted" not in sent[0]
    assert "conflict_check" not in text
    assert text.startswith("## SmartMemory Context\n- [semantic] KX-410 costs 1,200 EUR.")


def test_local_recall_with_the_flag_refuses_on_a_real_lite_store_with_contradicting_notes(tmp_path, monkeypatch):
    from unittest.mock import patch

    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory
    from smartmemory_mcp.backends.local import LocalBackend

    for name in ("OPENAI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "")
    sm = create_lite_memory(
        data_dir=str(tmp_path), pipeline_profile=PipelineConfig.lite(llm_enabled=False), spawn_worker=False
    )
    try:
        for row in _CONTRADICTING:
            sm.add(row["content"], memory_type="semantic")
        backend = LocalBackend.__new__(LocalBackend)
        backend._mem = sm
        with patch("smartmemory_app.storage.recall", return_value="never reached") as storage_recall:
            with pytest.raises(ValueError, match="fail_if_conflicted is not supported by LocalBackend.recall"):
                backend.recall(cwd="KX-410", top_k=5, fail_if_conflicted=True)
            storage_recall.assert_not_called()
            # Flag unset or False: the call is exactly what it was.
            assert backend.recall(cwd="KX-410", top_k=5) == "never reached"
            assert backend.recall(cwd="KX-410", top_k=5, fail_if_conflicted=False) == "never reached"
            assert storage_recall.call_args_list[0].args == ("KX-410", 5)
    finally:
        sm.close()
