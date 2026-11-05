"""Opt-in local real-service contract, using two pre-existing test-user credentials.

No hosted/production URL is accepted. Test credentials must be supplied by the
local service fixture owner. This test creates no identities, keys or memories.
"""

import os
from urllib.parse import urlparse
import httpx
import pytest
from smartmemory_mcp.hosted.server import _list_workspace_rows
from smartmemory_mcp.backends.remote import RemoteBackend


def test_two_real_users_workspace_listing_and_foreign_rejection(monkeypatch):
    base = os.getenv("MAYA_WORK_TEST_SERVICE_URL", "")
    tokens = [
        os.getenv("MAYA_WORK_TEST_USER_A_TOKEN"),
        os.getenv("MAYA_WORK_TEST_USER_B_TOKEN"),
    ]
    project = os.getenv(
        "MAYA_WORK_TEST_PROJECT_TEAM"
    )  # a workspace id the user may use
    foreign = os.getenv(
        "MAYA_WORK_TEST_FOREIGN_TEAM"
    )  # a workspace id the user may not
    if not base or not all(tokens) or not project or not foreign:
        pytest.skip(
            "requires local real-service two-user fixture and project/foreign workspace IDs"
        )
    assert urlparse(base).hostname in {"localhost", "127.0.0.1", "::1"}
    identities = []
    for token in tokens:
        with httpx.Client(
            base_url=base, headers={"Authorization": "Bearer " + token}, trust_env=False
        ) as client:
            me = client.get("/auth/me")
            me.raise_for_status()
            identities.append(me.json())
            listing = client.get("/memory/workspaces")
            listing.raise_for_status()
            rows = listing.json()["workspaces"]
            import smartmemory_mcp.hosted.server as hosted

            backend = RemoteBackend(api_url=base, api_key=token, team_id=project)
            monkeypatch.setattr(hosted, "get_backend", lambda: backend)
            offered, why = _list_workspace_rows()
            assert offered is not None, why
            allowed = {r["workspace_id"] for r in offered}
            # Workspaces only: a Team id is not a workspace id (MCP-SWITCH-WORKSPACE-NAMING-SWEEP-1).
            assert allowed == {r["workspace_id"] for r in rows}
            assert project in allowed and foreign not in allowed
    assert identities[0]["id"] != identities[1]["id"]
    assert identities[0]["tenant_id"] == identities[1]["tenant_id"]
