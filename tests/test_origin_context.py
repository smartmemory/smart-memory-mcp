"""MCP context.origin survives local/remote backend translation."""

import sys
import types
from unittest.mock import MagicMock

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend


def test_remote_context():
    backend = RemoteBackend(api_url="https://api.test", api_key="test", team_id="ws")
    backend._request = MagicMock(return_value={"item_id": "id"})
    context = {"origin": "import:obsidian", "source_path": "/vault/x"}
    backend.ingest("note", context=context, metadata={"origin": "api:ingest"})
    assert (
        backend._request.call_args.kwargs["json"]["context"]["origin"]
        == context["origin"]
    )
    backend.ingest_conversation_sync(
        [{"role": "user", "content": "hi"}], context=context
    )
    assert backend._request.call_args.kwargs["json"]["context"] == context


def test_local_context(monkeypatch):
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = MagicMock()
    context = {"origin": "import:obsidian", "source_path": "/vault/x"}
    backend.add("note", context=context)
    assert backend._mem.add.call_args.args[0].origin == context["origin"]
    storage = types.ModuleType("smartmemory_app.storage")
    storage.ingest = MagicMock(return_value="id")
    monkeypatch.setitem(sys.modules, "smartmemory_app.storage", storage)
    backend.ingest("note", context=context)
    assert storage.ingest.call_args.kwargs["origin"] == context["origin"]
    assert (
        storage.ingest.call_args.kwargs["properties"]["source_path"]
        == context["source_path"]
    )
