"""CORE-MEMORY-DYNAMICS-1 M1a / Task 6.1 — standalone MCP migration.

Mirrors the service repo's shim + new-tool tests with the standalone's
backend-based plumbing.  No FastMCP server; invokes the registered
functions via a capturing fake MCP.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_mcp.tools import memory_tools


@pytest.fixture(autouse=True)
def reset_deprecation_flag():
    memory_tools._RECALL_DEPRECATION_WARNED = False
    yield
    memory_tools._RECALL_DEPRECATION_WARNED = False


def _registered():
    captured = {}

    class _FakeMCP:
        def tool(self, *a, **kw):
            def _decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return _decorator

    memory_tools.register_free(_FakeMCP())
    return captured


def _mk_row(item_id, content, mtype, **extra):
    return {
        "item_id": item_id,
        "content": content,
        "memory_type": mtype,
        "metadata": extra.get("metadata", {}),
        "score": extra.get("score"),
    }


# --- _build_working_context helper ---------------------------------------- #


def test_build_working_context_produces_contract_shape():
    backend = MagicMock()
    backend.search.return_value = [_mk_row("w1", "hi", "pending")]

    resp = memory_tools._build_working_context(
        backend,
        session_id="s",
        query="q",
        k=5,
        max_tokens=None,
        strategy=None,
    )
    for key in (
        "decision_id",
        "items",
        "drift_warnings",
        "strategy_used",
        "tokens_used",
        "tokens_budget",
        "deprecation",
    ):
        assert key in resp
    assert resp["strategy_used"] == "fast:recency"
    assert resp["deprecation"] is None


def test_build_working_context_respects_max_tokens():
    backend = MagicMock()
    backend.search.return_value = [
        _mk_row("w1", "a" * 100, "pending"),
        _mk_row("w2", "b" * 100, "pending"),
        _mk_row("w3", "c" * 100, "pending"),
    ]
    # Budget of 30 → ~25 tokens per item; fits one.
    resp = memory_tools._build_working_context(
        backend,
        session_id="s",
        query="q",
        k=10,
        max_tokens=30,
        strategy=None,
    )
    assert len(resp["items"]) < 3
    assert resp["tokens_used"] <= 30


def test_build_working_context_budget_too_small_raises():
    backend = MagicMock()
    backend.search.return_value = [_mk_row("w1", "a" * 100, "pending")]
    with pytest.raises(ValueError, match="budget_too_small"):
        memory_tools._build_working_context(
            backend,
            session_id="s",
            query="q",
            k=5,
            max_tokens=1,
            strategy=None,
        )


# --- get_working_context tool --------------------------------------------- #


def test_get_working_context_tool_happy_path():
    tools = _registered()
    gwc = tools["get_working_context"]

    fake_backend = MagicMock()
    fake_backend.search.return_value = [_mk_row("w1", "hello", "pending")]

    with patch("smartmemory_mcp.tools.memory_tools.get_backend", return_value=fake_backend):
        resp = gwc(session_id="s1", query="hello")

    assert resp["strategy_used"] == "fast:recency"
    assert len(resp["items"]) >= 1


def test_strip_identity_metadata_removes_exact_identity_fields():
    item = {
        "content": "kept",
        "workspace_id": "top-workspace",
        "run_id": "top-run",
        "metadata": {
            "tenant_id": "tenant",
            "workspace_id": "workspace",
            "team_id": "team",
            "user_id": "user",
            "run_id": "run",
            "session_id": "legitimate-session",
            "tags": ["kept"],
        },
    }

    sanitized = memory_tools._strip_identity_metadata(item)

    assert sanitized == {
        "content": "kept",
        "metadata": {"session_id": "legitimate-session", "tags": ["kept"]},
    }
    assert item["workspace_id"] == "top-workspace"
    assert item["metadata"]["tenant_id"] == "tenant"


def test_get_working_context_strips_identity_metadata():
    tools = _registered()
    backend = MagicMock()
    backend.search.return_value = [
        _mk_row(
            "w1",
            "hello",
            "pending",
            metadata={"workspace_id": "secret", "team_id": "secret", "topic": "kept"},
        )
    ]

    with patch("smartmemory_mcp.tools.memory_tools.get_backend", return_value=backend):
        response = tools["get_working_context"](session_id="s1", query="hello")

    assert response["items"][0]["metadata"] == {"topic": "kept"}


def test_get_working_context_tool_rejects_bad_k():
    tools = _registered()
    gwc = tools["get_working_context"]
    with pytest.raises(ValueError, match="k must be"):
        gwc(session_id="s", query="q", k=0)
    with pytest.raises(ValueError, match="k must be"):
        gwc(session_id="s", query="q", k=101)


# --- memory_recall shim --------------------------------------------------- #


def test_memory_recall_deprecation_logged_once(caplog):
    tools = _registered()
    recall = tools["memory_recall"]

    fake_backend = MagicMock(spec=["search"])  # force non-native path
    fake_backend.search.return_value = [_mk_row("w1", "hi", "pending")]

    with patch("smartmemory_mcp.tools.memory_tools.get_backend", return_value=fake_backend):
        with caplog.at_level(logging.WARNING, logger=memory_tools.logger.name):
            recall("q", top_k=3)
            recall("q2", top_k=3)

    deprecation_logs = [r for r in caplog.records if "deprecated" in r.getMessage().lower()]
    assert len(deprecation_logs) == 1


def test_memory_recall_filters_to_working():
    tools = _registered()
    recall = tools["memory_recall"]

    fake_backend = MagicMock(spec=["search"])
    fake_backend.search.return_value = [
        _mk_row("w1", "working turn", "pending"),
        _mk_row("s1", "semantic fact", "semantic"),
        _mk_row("e1", "episodic event", "episodic"),
    ]
    with patch("smartmemory_mcp.tools.memory_tools.get_backend", return_value=fake_backend):
        out = recall("q", top_k=5)

    assert "working turn" in out
    assert "semantic fact" not in out
    assert "episodic event" not in out


def test_legacy_recall_type_scope_matches_service():
    """Plan-m1a.md allows collapsing to a shared constant if the two repos' scopes are identical.
    We verify here that they match the service's value.  If they diverge, this regression
    test forces the implementer to handle it explicitly per plan-m1a.md §2."""
    assert memory_tools._LEGACY_RECALL_TYPE_SCOPE == {"pending"}


def test_top_k_validation():
    tools = _registered()
    recall = tools["memory_recall"]
    assert recall("q", top_k=0) == "top_k must be at least 1."


def test_build_working_context_with_none_search_result():
    """Codex coverage: backend.search returning None must produce empty contract, no raise."""
    backend = MagicMock()
    backend.search.return_value = None
    resp = memory_tools._build_working_context(
        backend,
        session_id="s",
        query="q",
        k=5,
        max_tokens=None,
        strategy=None,
    )
    assert resp["items"] == []
    assert resp["tokens_used"] == 0
    assert resp["strategy_used"] == "fast:recency"


def test_build_working_context_with_empty_search_result():
    """Codex coverage: backend.search returning [] must also produce empty contract."""
    backend = MagicMock()
    backend.search.return_value = []
    resp = memory_tools._build_working_context(
        backend,
        session_id="s",
        query="q",
        k=5,
        max_tokens=None,
        strategy=None,
    )
    assert resp["items"] == []
    assert resp["tokens_used"] == 0


def test_build_working_context_exact_fit_budget():
    """Codex coverage: max_tokens exactly equals item token count — must INCLUDE
    the item (loop uses `>` for overflow, so `==` passes)."""
    backend = MagicMock()
    # content 40 chars → max(1, 40//4) = 10 tokens
    backend.search.return_value = [_mk_row("w1", "x" * 40, "pending")]
    resp = memory_tools._build_working_context(
        backend,
        session_id="s",
        query="q",
        k=5,
        max_tokens=10,
        strategy=None,
    )
    assert len(resp["items"]) == 1
    assert resp["tokens_used"] == 10


def test_build_working_context_empty_content_contributes_zero_tokens():
    """Codex coverage: zero-token item (empty content) consumes zero budget per
    standalone's _estimate_tokens contract (short-circuits to 0 on falsy input)."""
    backend = MagicMock()
    backend.search.return_value = [
        _mk_row("w1", "", "pending"),
        _mk_row("w2", "y" * 40, "pending"),
    ]
    resp = memory_tools._build_working_context(
        backend,
        session_id="s",
        query="q",
        k=5,
        max_tokens=10,
        strategy=None,
    )
    # Empty content = 0 tokens, 40-char content = 10 tokens → both fit within 10.
    assert len(resp["items"]) == 2
    assert resp["tokens_used"] == 10
