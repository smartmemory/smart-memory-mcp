"""Run against private backends with the service's real app and signup fixtures.

Copy this module, _attribution_notes.py, test_hosted_decisions_connector_notes.py and
_decisions_connector_notes.py to an isolated service tests/security package. Put the MCP
checkout on PYTHONPATH and set DECISIONS_CONNECTOR_API_PORT to the reserved API port.
"""

import pytest

pytest.importorskip("service")

from ._attribution_notes import exercise
from .test_hosted_decisions_connector_notes import (  # noqa: F401
    _toolset,
    api_url,
    hosted_tools,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cleanup_module_falkordb_graphs"),
]


def test_stated_attribution_on_hosted(hosted_tools, api_url, test_tenant, monkeypatch):  # noqa: F811
    """Exercise authenticated hosted writes and reads with immediate replacement enabled."""
    from service_common.workspaces import settings

    monkeypatch.setattr(
        settings, "runtime_supersession", lambda tenant_id, workspace_id: "retire"
    )
    tools, _ = hosted_tools
    exercise(_toolset(tools, api_url, test_tenant, None))
