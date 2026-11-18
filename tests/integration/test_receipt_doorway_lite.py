"""WRAPPER-2.0-RECEIPT-COLLAPSE-1: the save receipt reaches MCP ``memory_save`` on Lite, and ``storage.ingest`` keeps its id.

Real Lite store (temp dir), real core, wrapper and MCP tool; nothing synthesizes a receipt (the gap U4.42's suites left).
One table over both repos: the wrapper's legacy ``ingest`` (an id string, U4.32, unchanged), the wrapper's receipt
doorway ``ingest_with_receipt`` (core's receipt kept), MCP ``memory_save`` for a text save (the real receipt in the
answer) and for ``pending`` (stored as is: no receipt on Lite, and none made up). Every row reads the item back.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def lite_env(tmp_path, monkeypatch):
    import smartmemory_app.storage as storage

    monkeypatch.setenv("SMARTMEMORY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("SMARTMEMORY_EMBEDDING_BACKEND", "onnx")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_WARM", "1")
    monkeypatch.setenv("SMARTMEMORY_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("SMARTMEMORY_LLM_PROVIDER", "none")
    for prefix in ("GRAPH_DB", "FALKORDB", "REDIS", "CACHE_REDIS", "MONGODB"):
        monkeypatch.setenv(f"{prefix}_HOST", "127.0.0.1")
        monkeypatch.setenv(f"{prefix}_PORT", "1")
    for name in (
        "SMARTMEMORY_COSTLESS_ORIGINS",
        "SMARTMEMORY_WORKSPACE_ID",
        "SMARTMEMORY_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "smartmemory.pipeline.work_graph.spawn.spawn_worker_if_idle",
        lambda _data_dir: False,
    )
    storage._memory = None
    storage._data_path = None
    yield storage
    storage._shutdown()
    storage._memory = None
    storage._data_path = None


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


def _memory_save(monkeypatch, **kwargs):
    """Call the real ``memory_save`` tool on a real ``LocalBackend``; answer ``(item_id, receipt, summary, status)``."""
    from smartmemory_mcp.backends.local import LocalBackend
    from smartmemory_mcp.tools import save_tools

    backend = LocalBackend()
    monkeypatch.setattr(save_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    save_tools.register_free(mcp)
    answer = mcp.fns["memory_save"](**kwargs).structured_content
    return answer["item_id"], answer["receipt"], answer["summary"], answer["status"]


def _wrapper_ingest(storage, _monkeypatch, content):
    item_id = storage.ingest(content, "semantic")
    return item_id, None, None, None


def _wrapper_doorway(storage, _monkeypatch, content):
    saved = storage.ingest_with_receipt(content, "semantic")
    return saved.value, saved.receipt, saved.summary, None


def _mcp_semantic(_storage, monkeypatch, content):
    return _memory_save(monkeypatch, content=content)


def _mcp_pending(_storage, monkeypatch, content):
    return _memory_save(monkeypatch, content=content, memory_type="pending")


# (label, call, content, the id's type, a receipt expected, the status the tool answers or None for the wrapper rows)
ROWS = [
    (
        "wrapper ingest: legacy id string, unchanged",
        _wrapper_ingest,
        "Legacy ingest note gamma-3",
        str,
        False,
        None,
    ),
    (
        "wrapper ingest_with_receipt: core receipt kept",
        _wrapper_doorway,
        "Doorway note delta-4",
        str,
        True,
        None,
    ),
    (
        "mcp memory_save semantic: real receipt answered",
        _mcp_semantic,
        "Tool note epsilon-5",
        str,
        True,
        "queued",
    ),
    (
        "mcp memory_save pending: no receipt, none made up",
        _mcp_pending,
        "Pending note zeta-6",
        str,
        False,
        "saved",
    ),
]


@pytest.mark.parametrize(
    "label, call, content, id_type, has_receipt, status", ROWS, ids=[r[0] for r in ROWS]
)
def test_the_receipt_reaches_the_receipt_doorways_and_ingest_keeps_its_id(
    label, call, content, id_type, has_receipt, status, lite_env, monkeypatch
):
    item_id, receipt, summary, answered = call(lite_env, monkeypatch, content)
    assert type(item_id) is id_type, (label, item_id)
    if has_receipt:
        assert receipt["schema_version"] == "1.1", label
        assert receipt["placement"] == "lite", label
        assert receipt["subject"] == {"kind": "item_save", "item_id": item_id}, label
        assert receipt["status_url"] == f"local-status:{receipt['run_id']}", label
        assert summary.startswith(item_id), (label, summary)
    else:
        assert (receipt, summary) == (None, None), label
    assert answered == status, label
    assert lite_env.get_memory().get(item_id).content == content, label
