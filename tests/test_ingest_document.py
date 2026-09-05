"""Document ingestion must preserve the core and service contracts."""

from unittest.mock import Mock

import httpx
import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend


@pytest.mark.parametrize(
    "status,chunk_ids", [("ingested", ["c1", "c2"]), ("existing", [])]
)
@pytest.mark.parametrize(
    "options",
    [
        {},
        {
            "source_type": "markdown",
            "chunk_size": 500,
            "chunk_strategy": "recursive",
            "reference": True,
        },
    ],
)
def test_local_ingest_document(status, chunk_ids, options):
    result = {"document_id": "doc1", "chunk_ids": chunk_ids, "status": status}
    core = Mock(spec=["ingest_document"])
    core.ingest_document.return_value = result
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = core

    assert backend.ingest_document("/tmp/doc.md", **options) == result
    core.ingest_document.assert_called_once_with(
        "/tmp/doc.md",
        **(
            {
                "source_type": "auto",
                "chunk_size": 2000,
                "chunk_strategy": "paragraph",
                "reference": False,
            }
            | options
        ),
    )


@pytest.mark.parametrize(
    "status,chunk_ids", [("ingested", ["c1", "c2"]), ("existing", [])]
)
@pytest.mark.parametrize(
    "options",
    [
        {},
        {
            "source_type": "markdown",
            "chunk_size": 500,
            "chunk_strategy": "recursive",
            "reference": True,
        },
    ],
)
def test_remote_ingest_document(monkeypatch, status, chunk_ids, options):
    result = {
        "document_id": "doc1",
        "chunk_ids": chunk_ids,
        "status": status,
        "workspace_id": "ws1",
    }
    backend = RemoteBackend(api_url="https://api.test", api_key="test", team_id="ws1")
    backend._session["_bootstrapped"] = True
    client = Mock(
        return_value=httpx.Response(
            200,
            json=result,
            request=httpx.Request("POST", "https://api.test/memory/ingest/document"),
        )
    )
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", client)

    assert backend.ingest_document("https://example.com/doc.md", **options) == result
    client.assert_called_once_with(
        "POST",
        "https://api.test/memory/ingest/document",
        headers={
            "Authorization": "Bearer test",
            "Content-Type": "application/json",
            "X-Workspace-Id": "ws1",
        },
        timeout=300,
        json={
            "source": "https://example.com/doc.md",
            "source_type": "auto",
            "chunk_size": 2000,
            "chunk_strategy": "paragraph",
            "reference": False,
        }
        | options,
    )


@pytest.mark.parametrize("status", [422, 500])
def test_remote_ingest_document_surfaces_service_error(monkeypatch, status):
    backend = RemoteBackend(api_url="https://api.test", api_key="test", team_id="ws1")
    backend._session["_bootstrapped"] = True
    client = Mock(
        return_value=httpx.Response(
            status,
            json={"detail": "document rejected"},
            request=httpx.Request("POST", "https://api.test/memory/ingest/document"),
        )
    )
    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", client)

    with pytest.raises(RuntimeError, match=f"API error {status}.*document rejected"):
        backend.ingest_document("https://example.com/doc.md")


def test_local_ingest_document_propagates_loader_error():
    backend = LocalBackend.__new__(LocalBackend)
    backend._mem = Mock(spec=["ingest_document"])
    backend._mem.ingest_document.side_effect = ValueError("unknown source type")
    with pytest.raises(ValueError, match="unknown source type"):
        backend.ingest_document("/tmp/doc.md", source_type="invalid")
