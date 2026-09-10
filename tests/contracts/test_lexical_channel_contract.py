"""One paired replay across local tools, hosted override and remote transport."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.server import _register_hosted_tools
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import memory_tools

CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "smart-memory-docs/docs/features/CORE-LEXICAL-INDEX-1/lexical-contract.json"
    ).read_text()
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["local", "hosted", "remote"])
@pytest.mark.parametrize(
    "case",
    [
        "omitted",
        "empty",
        "zero",
        "accepted",
        "contains",
        "keyword-bm25",
        "unavailable",
        "validation",
        "connection",
    ],
)
async def test_all_mcp_search_paths_obey_lexical_contract(monkeypatch, path, case):
    options = {}
    if case in {"empty", "zero", "accepted", "contains", "keyword-bm25"}:
        options["channel_weights"] = (
            {}
            if case == "empty"
            else {"lexical": 0}
            if case == "zero"
            else dict.fromkeys(CONTRACT["channels"]["accepted"], 0.8)
            if case == "accepted"
            else {case: 0}
        )
    unavailable = case in {"unavailable", "connection", "validation"}
    removed = case in CONTRACT["channels"]["removed"]
    message = "LexicalIndexUnavailableError: connection refused; sm rebuild --lexical"
    seen = []

    def transport(method, url, **kwargs):
        seen.append(kwargs["json"])
        if case == "connection":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(
            400 if case == "validation" else 503 if unavailable else 200,
            json={"detail": message} if unavailable else {"results": []},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", transport)
    backend = RemoteBackend(api_url="http://test", api_key="fake", team_id="ws")
    backend._session["_bootstrapped"] = True
    if path == "remote":
        if unavailable or removed:
            with pytest.raises(ValueError if removed else RuntimeError) as error:
                backend.search("quartz", **options)
            if removed:
                assert "lexical" in str(error.value)
                assert not seen
        else:
            assert backend.search("quartz", **options) == []
            if case == "omitted":
                assert "channel_weights" not in seen[0]
            else:
                assert seen[0]["channel_weights"] == options["channel_weights"]
        return

    if path == "local":
        from smartmemory.graph.lexical import LexicalIndexUnavailableError

        backend = SimpleNamespace(search=Mock(return_value=[]))
        if unavailable:
            backend.search.side_effect = LexicalIndexUnavailableError(
                "falkordb", "search", "connection refused"
            )
    if path == "local" and case == "validation":
        backend.search.side_effect = ValueError(
            CONTRACT["errors"]["validation"]["unmatched_quote_message"]
        )
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
    mcp = FastMCP("lexical-contract", on_duplicate="replace")
    registrar = _CapturingRegistrar(mcp)
    memory_tools.register_free(registrar)
    if path == "hosted":
        _register_hosted_tools(mcp, registrar.captured, Mock())
    async with Client(mcp) as client:
        result = await client.call_tool(
            "memory_search", {"query": "quartz", **options}, raise_on_error=False
        )
        assert result.is_error == (unavailable or removed), result
    if path == "local":
        if removed:
            backend.search.assert_not_called()
        elif not unavailable:
            assert backend.search.call_args.kwargs["channel_weights"] == options.get(
                "channel_weights"
            )
    elif not (removed or unavailable):
        if case == "omitted":
            assert "channel_weights" not in seen[0]
        else:
            assert seen[0]["channel_weights"] == options["channel_weights"]
