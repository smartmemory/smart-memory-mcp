"""Exercise UTC defaults on registered MCP tools over a disposable Lite store."""

# === 1. IMPORTS ===
import asyncio
from tempfile import TemporaryDirectory

from fastmcp import Client
from fastmcp.exceptions import ToolError
from freezegun import freeze_time
import pytest

from smartmemory.models.decision import Decision
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from tests.test_scoped_validity import server_for
from tests.test_decision_tools_as_of import _remote, _tools

# === 2. TYPES & CONSTANTS ===
OLD = "Use the blue ledger"
NEW = "Use the green ledger"


# === 3. PUBLIC API ===
@pytest.fixture
def history(monkeypatch):
    """Record a rule and replace it the next day using real Lite lifecycle writes."""
    with TemporaryDirectory(prefix="test_asof_plain_date_") as store:
        memory = create_lite_memory(
            data_dir=store,
            pipeline_profile=PipelineConfig.lite_hermetic(),
            spawn_worker=False,
        )
        try:
            # 1. Record the original and replacement at known UTC instants.
            # Transformers exposes lazy modules whose dir() imports optional backends.
            with freeze_time("2026-02-28 12:00:00", ignore=["transformers"]):
                old = memory.add_decision(OLD).decision_id
            with freeze_time("2026-03-01 08:00:00", ignore=["transformers"]):
                memory.supersede_decision(old, Decision(content=NEW), reason="revision")

            # 2. Wire the registered tools to the same in-process store.
            backend = LocalBackend.__new__(LocalBackend)
            backend._mem = memory

            # 3. Let FastMCP perform its ordinary schema and response handling.
            yield Client(server_for(backend, monkeypatch)), old
        finally:
            memory.close()


def test_rule_at_close_of_each_date_and_at_explicit_times(history):
    """Pin which rule is active on either side of the March 1 replacement."""
    client, _ = history

    async def check():
        async with client:
            for stamp, expected, other, note in (
                (
                    "2026-02-28",
                    OLD,
                    NEW,
                    "as of 2026-02-28T23:59:59Z, date read as end of day UTC",
                ),
                (
                    "2026-03-01",
                    NEW,
                    OLD,
                    "as of 2026-03-01T23:59:59Z, date read as end of day UTC",
                ),
                (
                    "2026-03-01T09:12:00",
                    NEW,
                    OLD,
                    "as of 2026-03-01T09:12:00Z, time read as UTC",
                ),
                ("2026-03-01T09:12:00+08:00", OLD, NEW, None),
                ("2026-03-01T09:12:00Z", NEW, OLD, None),
                (None, NEW, OLD, None),
            ):
                args = {} if stamp is None else {"as_of": stamp}
                text = (await client.call_tool("decision_list", args)).content[0].text
                assert expected in text and other not in text, text
                if note:
                    assert note in text
                else:
                    assert "read as" not in text
                    if stamp:
                        assert stamp in text

    asyncio.run(check())


def test_all_dated_tools_describe_and_report_the_default(history):
    """Check the discoverable help and text or structured answer for each tool."""
    client, old = history
    calls = {
        "decision_get": {"decision_id": old},
        "decision_list": {},
        "decision_search": {"topic": "ledger"},
        "decision_provenance": {"decision_id": old},
        "decision_history": {"decision_id": old},
        "memory_recall_pack": {"budget_tokens": 300, "sections": [{"name": "notes"}]},
        "memory_policy_bundle": {},
    }

    async def check():
        async with client:
            descriptions = {
                tool.name: tool.description
                for tool in await client.list_tools()
                if "as_of" in tool.input_schema["properties"]
            }
            assert descriptions.keys() == calls.keys()
            for name, args in calls.items():
                assert "end of day UTC" in descriptions[name]
                note = "as of 2026-03-01T23:59:59Z, date read as end of day UTC"
                if name == "memory_policy_bundle":
                    with pytest.raises(
                        ToolError, match="not available on an unscoped store"
                    ) as error:
                        await client.call_tool(name, {**args, "as_of": "2026-03-01"})
                    assert note in str(error.value)
                    continue
                result = await client.call_tool(name, {**args, "as_of": "2026-03-01"})
                if name.startswith("memory_"):
                    assert result.data["as_of_note"] == note
                    assert "error" not in result.data
                else:
                    assert note in result.content[0].text

    asyncio.run(check())


@pytest.mark.parametrize("invalid", ["last Tuesday", "2026-02-30", "2026-03-01x09:12"])
def test_refusal_includes_the_input_and_two_examples(history, invalid):
    """Invalid calendar values and prose get an actionable tool error."""
    client, _ = history

    async def check():
        async with client:
            with pytest.raises(ToolError) as error:
                await client.call_tool("decision_list", {"as_of": invalid})
            assert invalid in str(error.value)
            assert "2026-03-01 or 2026-03-01T09:12:00+08:00" in str(error.value)

    asyncio.run(check())


def test_policy_bundle_preserves_structured_success_and_sends_resolved_instant(
    monkeypatch,
):
    """Check the remote tool's successful response using its offline transport fixture."""
    calls = _remote(monkeypatch)
    result = _tools()["memory_policy_bundle"](as_of="2026-03-01")
    assert calls == [
        ("GET", "/memory/policy/bundle", {"params": {"as_of": "2026-03-01T23:59:59Z"}})
    ]
    assert (
        result["as_of_note"]
        == "as of 2026-03-01T23:59:59Z, date read as end of day UTC"
    )
    assert "error" not in result
    assert result["workspace_id"] == "ws"
