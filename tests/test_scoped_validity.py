"""U12 wire/schema regressions. Golden behavior lives in the real-backend suite."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from smartmemory_mcp.tools import decision_tools, memory_tools

SCOPED_VALIDITY_VERSION = 2
CONTRACT_DIGEST = "26c6580effb9ac8e11e5471ef35489f66319f5419cc15d9f7e97781d0f5ba4eb"
FIXTURES_DIGEST = "a4756030405465024f25ba7ba1665b4fd8e1e3cf2972b63ae6e1c8107561aa07"
STAMP = "2026-02-08T00:00:00Z"
SCOPE = {"site": "B"}
SCHEDULE = [{"scope": {"site": "A"}, "valid_from": STAMP}]


def server_for(backend, monkeypatch):
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
    server = FastMCP("test_scoped_u12")
    decision_tools.register(server)
    memory_tools.register_free(server)
    memory_tools.register_pro(server)
    return server


def test_contract_and_fixture_pins():
    for key, expected in [("CONTRACT", CONTRACT_DIGEST), ("FIXTURE", FIXTURES_DIGEST)]:
        value = json.loads(Path(os.environ["SCOPED_VALIDITY_" + key]).read_text())
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(canonical).hexdigest() == expected
        assert value["version"] == SCOPED_VALIDITY_VERSION


def test_registered_schema_exposes_frozen_fields(monkeypatch):
    server = server_for(None, monkeypatch)

    async def check():
        async with Client(server) as client:
            schemas = {t.name: t.inputSchema for t in await client.list_tools()}
            for name in [
                "memory_search",
                "decision_get",
                "decision_list",
                "decision_search",
                "decision_provenance",
            ]:
                assert {"business_scope", "valid_at"} <= schemas[name][
                    "properties"
                ].keys(), name
            assert "scope_go_lives" in schemas["decision_create"]["properties"]
            assert {"scope_go_lives", "valid_from"} <= schemas["decision_supersede"][
                "properties"
            ].keys()

    asyncio.run(check())


@pytest.mark.parametrize("backend_class", [RemoteBackend, HostedRemoteBackend])
def test_decision_wire_axes_schedules_and_omission(monkeypatch, backend_class):
    # Only the serializer is isolated here, never a golden transport.
    calls = []
    backend = backend_class.__new__(backend_class)

    def capture(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/search") or path == "/memory/decisions":
            return {"decisions": []}
        return {
            "decision_id": "dec_test",
            "decision_type": "inference",
            "confidence": 0.8,
        }

    backend._decision_request = capture
    backend.decision_create("original", scope_go_lives=SCHEDULE)
    assert calls[-1][2]["json"]["scope_go_lives"] == SCHEDULE
    backend.decision_supersede("dec_test", "new", "amend", scope_go_lives=SCHEDULE)
    assert calls[-1][2]["json"]["scope_go_lives"] == SCHEDULE
    backend.decision_supersede("dec_test", "new", "amend", valid_from=STAMP)
    assert calls[-1][2]["json"]["valid_from"] == STAMP
    for method, args in [
        ("get", ["dec_test"]),
        ("list", []),
        ("search", ["CRF"]),
        ("provenance", ["dec_test"]),
    ]:
        fn = getattr(backend, "decision_" + method)
        fn(*args, as_of=STAMP, business_scope=SCOPE, valid_at=STAMP)
        params = calls[-1][2]["params"]
        assert json.loads(params["business_scope"]) == SCOPE
        assert params["valid_at"] == params["as_of"] == STAMP
        fn(*args)
        assert (
            not {"business_scope", "valid_at", "as_of"}
            & calls[-1][2].get("params", {}).keys()
        )
    backend.decision_create("legacy")
    assert "scope_go_lives" not in calls[-1][2]["json"]


def test_local_schedule_errors_are_not_absence():
    from smartmemory import ScopedValidityError

    backend = LocalBackend.__new__(LocalBackend)

    class Manager:
        def supersede(self, *args, **kwargs):
            raise ScopedValidityError("invalid_scoped_validity schedule")

    backend._decision_manager = lambda: Manager()
    with pytest.raises(ScopedValidityError):
        backend.decision_supersede("dec_test", "new", "amend")


def test_scoped_tool_replacement_does_not_claim_global_retirement(monkeypatch):
    class Scheduled:
        def decision_supersede(self, **kwargs):
            return {
                "status": "scheduled",
                "old_item_id": "v1",
                "new_item_id": "v2",
                "scope_go_lives": SCHEDULE,
            }

    server = server_for(Scheduled(), monkeypatch)

    async def check():
        async with Client(server) as client:
            result = await client.call_tool(
                "decision_supersede",
                {
                    "decision_id": "v1",
                    "new_content": "amended",
                    "reason": "rollout",
                    "scope_go_lives": SCHEDULE,
                },
            )
            text = result.content[0].text
            assert "scheduled" in text.lower()
            assert "v2" in text
            assert "v1 superseded" not in text

    asyncio.run(check())


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("decision_create", {"content": "invalid", "scope_go_lives": []}),
        ("decision_create", {"content": "invalid", "scope_go_lives": SCHEDULE * 2}),
        (
            "decision_create",
            {
                "content": "invalid",
                "scope_go_lives": [{"scope": None, "valid_from": "2026-02-01"}],
            },
        ),
        (
            "decision_supersede",
            {
                "decision_id": "old",
                "new_content": "invalid",
                "reason": "test",
                "scope_go_lives": SCHEDULE,
                "valid_from": STAMP,
            },
        ),
        ("decision_get", {"decision_id": "old", "business_scope": {"site": " "}}),
        ("decision_list", {"business_scope": {"site": "A", "country": "US"}}),
        ("decision_search", {"topic": "CRF", "valid_at": "bad"}),
        (
            "decision_provenance",
            {"decision_id": "old", "business_scope": {"site": "?"}},
        ),
        ("memory_search", {"query": "", "business_scope": {"country": "US"}}),
        ("memory_recall", {"query": "CRF", "valid_at": STAMP}),
        ("memory_recall_pack", {"budget_tokens": 100, "business_scope": SCOPE}),
        ("memory_policy_bundle", {"valid_at": STAMP}),
        ("memory_search_advanced", {"query": "CRF", "business_scope": SCOPE}),
        (
            "memory_search_by_metadata",
            {"metadata_key": "x", "metadata_value": "y", "valid_at": STAMP},
        ),
    ],
)
def test_registered_invalid_or_unsupported_inputs_refuse_before_dispatch(
    monkeypatch, tool, arguments
):
    from fastmcp.exceptions import ToolError

    server = server_for(None, monkeypatch)

    async def check():
        async with Client(server) as client:
            with pytest.raises(ToolError, match="invalid_scoped_validity"):
                await client.call_tool(tool, arguments)

    asyncio.run(check())
