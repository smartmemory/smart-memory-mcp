"""Opt-in local real-service contract, using two pre-existing test-user credentials.

No hosted/production URL is accepted. Test credentials must be supplied by the
local service fixture owner. This test creates no identities, keys or memories.
"""

import os
from urllib.parse import urlparse
import httpx
import pytest
from smartmemory_mcp.hosted.server import _list_team_ids
from smartmemory_mcp.backends.remote import RemoteBackend


def test_two_real_users_team_response_and_foreign_rejection(monkeypatch):
    base = os.getenv("MAYA_WORK_TEST_SERVICE_URL", "")
    tokens = [
        os.getenv("MAYA_WORK_TEST_USER_A_TOKEN"),
        os.getenv("MAYA_WORK_TEST_USER_B_TOKEN"),
    ]
    project = os.getenv("MAYA_WORK_TEST_PROJECT_TEAM")
    foreign = os.getenv("MAYA_WORK_TEST_FOREIGN_TEAM")
    if not base or not all(tokens) or not project or not foreign:
        pytest.skip(
            "requires local real-service two-user fixture and project/foreign Team IDs"
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
            teams = client.get("/memory/teams")
            teams.raise_for_status()
            rows = teams.json()["teams"]
            import smartmemory_mcp.hosted.server as hosted

            backend = RemoteBackend(api_url=base, api_key=token, team_id=project)
            monkeypatch.setattr(hosted, "get_backend", lambda: backend)
            allowed = _list_team_ids()
            assert allowed == {r["team_id"] for r in rows}
            assert project in allowed and foreign not in allowed
            response = client.get("/memory/teams/" + foreign)
            assert response.status_code in {403, 404}
    assert identities[0]["id"] != identities[1]["id"]
    assert identities[0]["tenant_id"] == identities[1]["tenant_id"]
