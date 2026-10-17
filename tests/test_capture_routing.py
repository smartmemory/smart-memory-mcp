"""Write-tool routing guidance and the memory_add skip warning (MCP-CAPTURE-ROUTING-1).

Owner report 2026-10-17: decisions and dated statements came back as plain items. Part of
the cause was routing: no server instructions, a memory_add docstring that was false on
the hosted service, and a decision_create docstring that never said when to use it. These
tests pin the guidance a model sees and the WARNING that names what a local memory_add loses.
"""

from __future__ import annotations

import logging
import re

import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS
from smartmemory_mcp.instructions import HOSTED_INSTRUCTIONS, LOCAL_INSTRUCTIONS
from smartmemory_mcp.tools import common, decision_tools, memory_tools

from ._hosted_fixtures import hosted_config, memory_store

SKIP_WARNING = "stores without the extraction pipeline"


class _Registrar:
    def __init__(self):
        self.functions = {}

    def tool(self, *args, **kwargs):
        def capture(function):
            self.functions[function.__name__] = function
            return function

        return capture


def _fake_backend(runs_extraction):
    """A backend whose add() records the call; runs_extraction None = attribute absent."""
    attrs = {
        "calls": [],
        "add": lambda self, content, **kw: self.calls.append(content) or "item-1",
    }
    if runs_extraction is not None:
        attrs["add_runs_extraction"] = runs_extraction
    return type("FakeBackend", (), attrs)()


# One row per backend shape memory_add can meet.
ADD_ROWS = [
    # (label, add_runs_extraction, expect_warning)
    ("local-like (no extraction)", False, True),
    ("remote-like (service pipeline)", True, False),
    ("unknown backend, attribute absent", None, True),
]


@pytest.mark.parametrize(("label", "runs_extraction", "expect_warning"), ADD_ROWS)
def test_memory_add_warns_only_when_extraction_is_skipped(
    monkeypatch, caplog, label, runs_extraction, expect_warning
):
    backend = _fake_backend(runs_extraction)
    monkeypatch.setattr(common, "_backend", backend)
    registrar = _Registrar()
    memory_tools.register_pro(registrar)

    with caplog.at_level(logging.WARNING, logger=memory_tools.logger.name):
        out = registrar.functions["memory_add"](
            "We ship the beta on November 3rd 2026."
        )

    assert backend.calls == ["We ship the beta on November 3rd 2026."], label
    assert "item-1" in out, label
    warned = [r for r in caplog.records if SKIP_WARNING in r.getMessage()]
    assert bool(warned) is expect_warning, label
    if warned:
        message = warned[0].getMessage()
        assert warned[0].levelno == logging.WARNING
        for lost in ("entities", "dates", "Decision nodes"):
            assert lost in message, (label, lost)


# The real backends declare the flag the tool reads.
BACKEND_ROWS = [(LocalBackend, False), (RemoteBackend, True)]


@pytest.mark.parametrize(("cls", "expected"), BACKEND_ROWS)
def test_backends_declare_whether_add_extracts(cls, expected):
    assert cls.add_runs_extraction is expected


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
    # The hosted allowlist has no decision tools; steering a hosted model to one would
    # send it to a tool it cannot call.
    mentioned = _mentioned_tools(HOSTED_INSTRUCTIONS)
    assert mentioned, "hosted instructions name no tools"
    assert mentioned <= HOSTED_TOOLS, mentioned - HOSTED_TOOLS
    assert "decision_create" not in HOSTED_TOOLS


def test_local_instructions_route_decisions_and_dates():
    assert "decision_create" in LOCAL_INSTRUCTIONS
    assert "I decided" in LOCAL_INSTRUCTIONS
    assert "dates" in LOCAL_INSTRUCTIONS and "memory_ingest" in LOCAL_INSTRUCTIONS


def test_tool_descriptions_say_when_to_use_them():
    registrar = _Registrar()
    memory_tools.register_free(registrar)
    memory_tools.register_pro(registrar)
    decision_tools.register(registrar)
    docs = {name: fn.__doc__ for name, fn in registrar.functions.items()}

    assert "without the NLP pipeline" not in docs["memory_add"]
    assert "hosted" in docs["memory_add"] and "local mode" in docs["memory_add"]
    assert "dates" in docs["memory_ingest"]
    assert "I decided" in docs["decision_create"]
    assert "CORE-EXPERTISE" not in docs["decision_create"]
