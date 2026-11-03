"""Account reporting preserves local model setup failures."""

import logging
from unittest.mock import Mock

import pytest

from smartmemory.errors import MissingModelError
from smartmemory_mcp.tier import Tier
from tests._tools import tool_fn


@pytest.fixture(autouse=True)
def _no_cached_backend(monkeypatch):
    # whoami reads the backend the memory tools use (tools.common.get_backend).
    monkeypatch.setattr("smartmemory_mcp.tools.common._backend", None)


def test_whoami_missing_model_surfaces_setup_message(monkeypatch, caplog):
    message = "Embedding model unavailable. Run sm setup."
    monkeypatch.setattr("smartmemory_mcp.server.resolve_tier", lambda: Tier.FREE)
    monkeypatch.setattr(
        "smartmemory_mcp.tools.common.resolve_backend",
        Mock(side_effect=MissingModelError(message)),
    )
    with caplog.at_level(logging.WARNING):
        result = tool_fn("whoami")()
    assert message in result
    assert "Not logged in" not in result
    assert any(
        r.levelno == logging.WARNING and message in r.message for r in caplog.records
    )


def test_whoami_unresolved_account_still_reports_login(monkeypatch):
    monkeypatch.setattr("smartmemory_mcp.server.resolve_tier", lambda: Tier.FREE)
    monkeypatch.setattr(
        "smartmemory_mcp.tools.common.resolve_backend",
        Mock(side_effect=RuntimeError("no account")),
    )
    assert "Not logged in" in tool_fn("whoami")()


def test_whoami_reports_resolved_backend(monkeypatch):
    monkeypatch.setattr("smartmemory_mcp.server.resolve_tier", lambda: Tier.FREE)
    monkeypatch.setattr(
        "smartmemory_mcp.tools.common.resolve_backend",
        lambda: Mock(whoami=Mock(return_value="Backend: local")),
    )
    assert tool_fn("whoami")() == "Tier: FREE\nBackend: local"
