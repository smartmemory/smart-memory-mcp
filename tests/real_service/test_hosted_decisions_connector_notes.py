"""OUTREACH-MARIUS-HITL-SURFACE-1 part 4, the frozen notes against the HOSTED connector's tool code and the real service.

Real service app (``service.app`` after its real startup) served over HTTP by uvicorn on a reserved test port, real
auth (two signed-up tenants), throwaway FalkorDB, Redis and Mongo, both workspaces in ``propose``. The hosted connector's own registry
(``register_module_tools`` + ``apply_allowlist``) supplies the tools; each call runs with a ``HostedIdentity`` bound
the way ``HostedIdentityMiddleware`` binds it (``build_hosted_backend`` for the signed-in user and workspace), so
``get_backend()`` is the per-call ``HostedRemoteBackend``. The Python SDK (``SmartMemoryClient.list_decisions``)
answers notes 5 and 6 too.

How to run: this file needs the service's own ``tests/conftest.py`` fixtures (``real_app``, ``base_client``,
``test_tenant``, ``second_tenant``), so it is copied with ``_decisions_connector_notes.py`` into a service checkout's
``tests/security/`` and run there with this repo and smart-memory-client on PYTHONPATH and
``DECISIONS_CONNECTOR_API_PORT`` set to a reserved test port. In this repo's own run it skips (no ``service``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("service")
pytest.importorskip("service_common.testing")

# The tenants' graphs this module creates are dropped at module end (the service's graph-leak gate checks it).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cleanup_module_falkordb_graphs"),
]

_spec = importlib.util.spec_from_file_location(
    "_decisions_connector_notes",
    Path(__file__).with_name("_decisions_connector_notes.py"),
)
notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notes)


@pytest.fixture(scope="module")
def api_url(real_app):
    import uvicorn

    port = int(os.environ["DECISIONS_CONNECTOR_API_PORT"])
    assert port not in (9010, 9012, 9013, 9444) and 19000 <= port <= 19999, port
    server = uvicorn.Server(
        uvicorn.Config(
            real_app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def hosted_tools():
    """The hosted registry, filtered by the hosted allowlist, as the hosted server builds it."""
    from fastmcp import FastMCP

    from smartmemory_mcp.hosted.identity import set_hosted_mode
    from smartmemory_mcp.hosted.tools import apply_allowlist, register_module_tools

    mcp = FastMCP("hosted-notes")
    captured = register_module_tools(mcp)
    apply_allowlist(mcp)
    advertised = {tool.name for tool in asyncio.run(mcp.list_tools())}
    set_hosted_mode(True)
    try:
        yield (
            {name: captured[name].function for name in advertised if name in captured},
            advertised,
        )
    finally:
        set_hosted_mode(False)


def _workspace_id(tenant: dict) -> str:
    return tenant.get("workspace_id") or tenant["team_id"]


@pytest.fixture(scope="module", autouse=True)
def _propose_workspaces():
    """Both workspaces state ``propose``, as the settings route would store it.

    The throwaway backends have no settings store (Postgres), so ``PATCH /memory/workspaces/{id}/settings`` answers
    503 ``settings_store_unavailable`` here. The service's own held-route test states the policy the same way
    (``tests/security/test_decision_supersede_held_route.py``): the one read the service makes is answered ``propose``.
    """
    from service_common.workspaces import settings

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            settings, "runtime_supersession", lambda tenant_id, workspace_id: "propose"
        )
        yield


def _toolset(tools: dict, api: str, tenant: dict, sdk) -> dict:
    """Each call runs with this tenant's identity bound, exactly as the hosted middleware binds it."""
    from smartmemory_mcp.hosted.exchange import ExchangeCache
    from smartmemory_mcp.hosted.identity import (
        HostedIdentity,
        VerifiedIdentity,
        build_hosted_backend,
        current_identity,
    )

    wid = _workspace_id(tenant)
    verified = VerifiedIdentity(
        kind="oauth",
        subject=tenant["user_id"],
        sm_access_token=tenant["access_token"],
        sm_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        user_id=tenant["user_id"],
        tenant_id=tenant["tenant_id"],
        email=tenant.get("email") or "",
        default_team_id=wid,
        active_workspace_id=wid,
        fingerprint=f"notes-{tenant['user_id']}",
    )
    cache = ExchangeCache()

    def bind(fn):
        def call(**kwargs):
            backend = build_hosted_backend(verified, wid, api_url=api, cache=cache)
            token = current_identity.set(
                HostedIdentity(verified=verified, team_id=wid, backend=backend)
            )
            try:
                return fn(**kwargs)
            finally:
                current_identity.reset(token)

        return call

    bound = {name: bind(fn) for name, fn in tools.items()}

    def retract(decision_id: str, reason: str) -> str:
        # decision_retract is not a hosted tool: the rule is retracted by the app (SDK), then asked about.
        sdk.retract_decision(decision_id, reason=reason)
        return f"Decision retracted: {decision_id}"

    bound.setdefault("decision_retract", retract)
    return bound


@pytest.fixture
def arms(hosted_tools, api_url, test_tenant, second_tenant):
    from smartmemory_client import SmartMemoryClient

    tools, _ = hosted_tools
    sdks = [
        SmartMemoryClient(
            base_url=api_url, token=t["access_token"], workspace_id=_workspace_id(t)
        )
        for t in (test_tenant, second_tenant)
    ]
    try:
        yield (
            _toolset(tools, api_url, test_tenant, sdks[0]),
            _toolset(tools, api_url, second_tenant, sdks[1]),
            sdks,
        )
    finally:
        for sdk in sdks:
            sdk.close()


class TestHostedDecisionsConnectorNotes:
    def test_the_review_tools_are_advertised_on_the_hosted_connector(
        self, hosted_tools
    ):
        _, advertised = hosted_tools
        assert {"review_queue_list", "review_queue_settle"} <= advertised
        assert not {"supersession_policy_get", "supersession_policy_set"} & advertised

    @pytest.mark.parametrize("note", notes.NOTES, ids=lambda n: n.__name__)
    def test_note_on_the_hosted_connector(self, arms, note, test_tenant):
        w1, w2, _ = arms
        user_id = test_tenant["user_id"]
        note(w1, w2, lambda settled: user_id in settled and "local:mcp" not in settled)

    def test_sdk_lists_replaced_and_retracted_decisions(self, arms):
        w1, _, (sdk1, sdk2) = arms
        old_text, new_text = notes.accepted(w1)
        gone = notes.retracted(w1)
        replaced = [
            d["content"] for d in sdk1.list_decisions(status="superseded", limit=200)
        ]
        assert old_text in replaced and new_text not in replaced, replaced
        withdrawn = [
            d["content"] for d in sdk1.list_decisions(status="retracted", limit=200)
        ]
        assert gone in withdrawn and old_text not in withdrawn, withdrawn
        in_force = [d["content"] for d in sdk1.list_decisions(limit=200)]
        assert (
            new_text in in_force and old_text not in in_force and gone not in in_force
        ), in_force
        for state in ("superseded", "retracted"):
            other = [d["content"] for d in sdk2.list_decisions(status=state, limit=200)]
            assert not {old_text, new_text, gone} & set(other), (state, other)
        with pytest.raises(ValueError, match="active, superseded, retracted, pending"):
            sdk1.list_decisions(status="deleted")
