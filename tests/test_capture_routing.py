"""Write-tool routing guidance (MCP-CAPTURE-ROUTING-1, ported to the 2.0 tool surface by FWD-MERGE-4).

Owner report 2026-10-17: decisions and dated statements came back as plain items. Part of
the cause was routing: no server instructions and a decision_create docstring that never
said when to use it. These tests pin the guidance a model sees. On 2.0 the write tools are
memory_save / memory_save_document / memory_save_conversation (U4.42-FU-TOOLS); main's
memory_add skip-warning rows have no 2.0 tool to run on (memory_save extracts free text on
every backend, `save_tools.RAW_MEMORY_TYPES` aside) and are not carried.
"""

from __future__ import annotations

import re

import anyio
import pytest
from fastmcp import Client

from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS
from smartmemory_mcp.instructions import HOSTED_INSTRUCTIONS, LOCAL_INSTRUCTIONS
from smartmemory_mcp.tools import decision_tools, save_tools

from ._hosted_fixtures import hosted_config, memory_store


class _Registrar:
    def __init__(self):
        self.functions = {}

    def tool(self, *args, **kwargs):
        def capture(function):
            self.functions[function.__name__] = function
            return function

        return capture


@pytest.fixture
def _restore_hosted_mode():
    before = hosted_identity.hosted_mode_enabled()
    try:
        yield
    finally:
        hosted_identity.set_hosted_mode(before)


def _mentioned_tools(text):
    return set(re.findall(r"\b(?:memory|decision)_[a-z_]+\b", text))


def test_servers_send_their_routing_instructions(_restore_hosted_mode):
    hosted = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )
    hosted_identity.set_hosted_mode(False)
    from smartmemory_mcp.server import mcp as local_server

    assert hosted.instructions == HOSTED_INSTRUCTIONS
    assert local_server.instructions == LOCAL_INSTRUCTIONS


def test_hosted_instructions_name_only_hosted_tools():
    # Every instruction must name a registered tool, including the scoped decision suite.
    mentioned = _mentioned_tools(HOSTED_INSTRUCTIONS)
    assert mentioned, "hosted instructions name no tools"
    assert mentioned <= HOSTED_TOOLS, mentioned - HOSTED_TOOLS
    assert {
        "decision_create",
        "decision_supersede",
        "decision_get",
        "decision_list",
        "decision_search",
        "decision_provenance",
    } <= HOSTED_TOOLS
    # The hosted server registers decision_create, so the text must route to it and not deny it exists.
    assert "decision_create" in mentioned and "decision_supersede" in mentioned
    assert "no decision tools" not in HOSTED_INSTRUCTIONS.lower()


def test_local_instructions_route_decisions_and_dates():
    assert "decision_create" in LOCAL_INSTRUCTIONS
    assert "I decided" in LOCAL_INSTRUCTIONS
    assert "dates" in LOCAL_INSTRUCTIONS and "memory_save" in LOCAL_INSTRUCTIONS


RETIRED_WRITE_TOOLS = ("memory_ingest", "memory_add")


@pytest.mark.parametrize("text", [LOCAL_INSTRUCTIONS, HOSTED_INSTRUCTIONS])
def test_instructions_never_name_a_retired_write_tool(text):
    # FWD-MERGE-4: main's instructions named memory_ingest / memory_add, which 2.0 does not register.
    mentioned = _mentioned_tools(text)
    assert not mentioned & set(RETIRED_WRITE_TOOLS), mentioned


def test_tool_descriptions_say_when_to_use_them():
    registrar = _Registrar()
    save_tools.register_free(registrar)
    decision_tools.register(registrar)
    docs = {name: fn.__doc__ for name, fn in registrar.functions.items()}

    assert "extraction pipeline" in docs["memory_save"]
    assert "pending" in docs["memory_save"]
    assert "I decided" in docs["decision_create"]
    assert "CORE-EXPERTISE" not in docs["decision_create"]
    for retired in RETIRED_WRITE_TOOLS:
        assert retired not in docs["decision_create"], retired


READ_LINE = (
    "Asked what was decided or which rule applies: use decision_search first (it lists current "
    "decisions only), then decision_get or decision_provenance for what a decision replaced and why. "
    "memory_ask answers carry a current-decision record when one matches."
)
DOES_FIRST = {
    "decision_search": "Search the current decisions",
    "decision_get": "Get one decision by id",
    "decision_provenance": "Show what a decision replaced",
}
SCOPE_PARAGRAPH = (
    "Business validity uses business_scope (exact site) and valid_at.\n\n"
    "Exact activated site entries take precedence, otherwise use sponsor fallback.\n"
    "Omitted valid_at is resolved once at request start. as_of is independent\n"
    "transaction-time history."
)


def _handshake(server):
    """initialize + tools/list through the fastmcp protocol client, as an assistant sends them."""

    async def run():
        async with Client(server) as client:
            tools = {tool.name: tool.description for tool in await client.list_tools()}
            return client.instructions, tools

    return anyio.run(run)


def test_read_questions_route_to_decision_tools_on_both_servers(_restore_hosted_mode):
    """SAME-ANSWER-U2 T1-T5: both servers send the read line and lead with what each tool does."""
    hosted = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )
    hosted_identity.set_hosted_mode(False)
    from smartmemory_mcp.server import mcp as local_server

    for server, text, registered in (
        (hosted, HOSTED_INSTRUCTIONS, set(HOSTED_TOOLS)),
        (local_server, LOCAL_INSTRUCTIONS, None),
    ):
        instructions, tools = _handshake(server)
        registered = registered if registered is not None else set(tools)
        # T1/T2: the read line reaches the client at initialize.
        assert instructions == text
        assert READ_LINE in instructions
        # T4: every named tool is registered on that server.
        named = _mentioned_tools(instructions) - {
            "memory_type"
        }  # a parameter, not a tool
        assert named <= registered
        assert named <= set(tools)
        # T3: the description opens with what the tool does, then the scope text, then as_of help.
        for name, opening in DOES_FIRST.items():
            description = tools[name]
            assert description.startswith(opening), (name, description[:80])
            assert SCOPE_PARAGRAPH in description, name
            assert description.index(SCOPE_PARAGRAPH) > len(opening)
            assert "end of day UTC" in description, name
        # T5: memory_ask says where the decision record is and what a failed lane means.
        ask = tools["memory_ask"]
        assert "``decision``" in ask or "`decision`" in ask
        assert "evidence item 1" in ask
        assert "lanes_failed" in ask and "partial" in ask
        # T6: decision_list already opened with what it does; it is untouched.
        assert not tools["decision_list"].startswith("Business validity")
