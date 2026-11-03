"""Opt-in local real-service contract, using two pre-existing test-user credentials.

No hosted/production URL is accepted. Test credentials must be supplied by the
local service fixture owner. This test creates no identities, keys or memories.
"""

import os
from urllib.parse import urlparse
import httpx
import pytest
from smartmemory_mcp.hosted.server import _list_team_rows
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
            # 2.0's hosted switch lists Teams (`_list_team_rows`, its KNOWN CONFLATION note): main moved it to
            # /memory/workspaces with WS-ROUTING-5, which FWD-MERGE-4 keeps off 2.0 (owner shape B). The workspace
            # listing port is MCP-2.0-SWITCH-WORKSPACE-NAMING-TWIN-1.
            listing = client.get("/memory/teams")
            listing.raise_for_status()
            rows = listing.json()["teams"]
            import smartmemory_mcp.hosted.server as hosted

            backend = RemoteBackend(api_url=base, api_key=token, team_id=project)
            monkeypatch.setattr(hosted, "get_backend", lambda: backend)
            offered, why = _list_team_rows()
            assert offered is not None, why
            allowed = {r["team_id"] for r in offered}
            assert allowed == {r["team_id"] for r in rows}
            assert project in allowed and foreign not in allowed
    assert identities[0]["id"] != identities[1]["id"]
    assert identities[0]["tenant_id"] == identities[1]["tenant_id"]
