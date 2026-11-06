"""Registered tools to real Lite, remote HTTP and hosted authenticated HTTP."""

import ast
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError
import httpx
import pytest

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.backend import HostedRemoteBackend
from tests.test_scoped_validity import server_for


@pytest.fixture(params=["local", "remote", "hosted"])
def runtime(request, tmp_path, monkeypatch):
    mode = request.param
    memory = None
    owners = json.loads(os.environ["SCOPED_VALIDITY_TEST_OWNERS"])
    url = os.environ["SCOPED_VALIDITY_SERVICE_URL"]
    if mode == "local":
        from smartmemory.pipeline.config import PipelineConfig
        from smartmemory.tools.factory import create_lite_memory
        from smartmemory_app import storage

        memory = create_lite_memory(
            data_dir=str(tmp_path / "test_scoped_u12"),
            pipeline_profile=PipelineConfig.lite_hermetic(),
            spawn_worker=False,
        )
        memory._reranker_config.enabled = False
        monkeypatch.setattr(storage, "get_memory", lambda: memory)
        backend = LocalBackend.__new__(LocalBackend)
        backend._mem = memory
    else:
        # Each mode owns its workspace, so top-one search cannot hit a sibling's fixture.
        with httpx.Client(base_url=url) as http:
            response = http.post(
                "/memory/teams",
                headers=owners[0]["headers"],
                json={"name": "test_scoped_u12_" + uuid4().hex},
            )
            assert response.status_code == 200, response.text
            workspace = response.json()["team_id"]
        if mode == "hosted":
            from smartmemory_mcp.hosted.exchange import ExchangeCache

            backend = HostedRemoteBackend(
                url,
                owners[0]["access_token"],
                workspace,
                fingerprint="test_scoped_u12",
                cache=ExchangeCache(),
            )
        else:
            backend = RemoteBackend(
                api_url=url, api_key=owners[0]["access_token"], team_id=workspace
            )
    try:
        yield mode, backend, memory, server_for(backend, monkeypatch), owners, url
    finally:
        if memory:
            memory.close()


def test_G01_G08_G11_G12_G14_G16_registered_parity(runtime):
    mode, backend, memory, server, owners, url = runtime
    card = json.loads(Path(os.environ["SCOPED_VALIDITY_FIXTURE"]).read_text())

    async def run():
        async with Client(server) as client:

            async def call(name, **kwargs):
                result = await client.call_tool(name, kwargs)
                return "\n".join(
                    block.text for block in result.content if hasattr(block, "text")
                )

            created = await call(
                "decision_create",
                content=card["versions"][0]["content"],
                scope_go_lives=card["versions"][0]["scope_go_lives"],
            )
            old = created.splitlines()[0].split(": ", 1)[1]
            before = datetime.now(timezone.utc).isoformat()
            replaced = await call(
                "decision_supersede",
                decision_id=old,
                new_content=card["versions"][1]["content"],
                reason="clinical amendment",
                scope_go_lives=card["versions"][1]["scope_go_lives"],
            )
            assert "scheduled" in replaced.lower()
            new = replaced.split("New decision: ")[1].splitlines()[0]
            ids = {"v1": old, "v2": new}
            # G01-G05 and G11 read-back: all canonical exact boundaries, unknown site and sponsor.
            for row in card["queries"]:
                options = {"valid_at": row["valid_at"]}
                if row["site"] is not None:
                    options["business_scope"] = {"site": row["site"]}
                selected = ast.literal_eval(
                    await call("decision_get", decision_id=new, **options)
                )
                assert selected["decision_id"] == ids[row["expected"]], row
                assert selected["content"] == next(
                    v["content"] for v in card["versions"] if v["id"] == row["expected"]
                )
                assert selected["scope_go_lives"]
                assert selected["as_of_resolution"] == "resolved"
            options = {
                "business_scope": {"site": "B"},
                "valid_at": "2026-02-08T00:00:00Z",
            }
            # G06 actual wrapper and service transit, including the strongest hit being the wrong version.
            for name, args in [
                ("decision_list", {}),
                ("decision_search", {"topic": "CRF guidance"}),
                ("decision_provenance", {"decision_id": new}),
            ]:
                text = await call(name, **args, **options)
                assert old in text and "Original CRF guidance" in text
                assert "as_of_resolution" in text
            from smartmemory.search.channels import CHANNEL_DEFAULTS

            lexical = {name: float(name == "lexical") for name in CHANNEL_DEFAULTS}

            async def search(**opts):
                text = await call(
                    "memory_search",
                    query="Amended CRF guidance",
                    top_k=1,
                    memory_type="decision",
                    enable_hybrid=False,
                    channel_weights=lexical,
                    cite=True,
                    **opts,
                )
                return json.loads(text)["items"]

            hits = await search(**options)
            assert [h["item_id"] for h in hits] == [old]
            assert hits[0]["content"] == "Original CRF guidance"
            assert hits[0]["scoped_validity_resolution"]["selected_item_id"] == old
            # The ordinary text and default catalog must carry the same selected identity and diagnostics.
            for catalog in (True, False):
                text = await call(
                    "memory_search",
                    query="Amended CRF guidance",
                    top_k=1,
                    memory_type="decision",
                    enable_hybrid=False,
                    channel_weights=lexical,
                    catalog_mode=catalog,
                    **options,
                )
                assert old in text and "Original CRF guidance" in text
                assert "scoped_validity_resolution" in text
            # G12 independent transaction axis and schedule revision, through the registered tools.
            at_a = {"business_scope": {"site": "A"}, "valid_at": options["valid_at"]}
            assert (
                ast.literal_eval(
                    await call("decision_get", decision_id=old, as_of=before, **at_a)
                )["decision_id"]
                == old
            )
            assert [h["item_id"] for h in await search(as_of_date=before, **at_a)] == [
                old
            ]
            known_before = datetime.now(timezone.utc).isoformat()
            correction = [
                {"scope": {"site": "A"}, "valid_from": "2026-02-10T00:00:00Z"},
                {"scope": {"site": "B"}, "valid_from": "2026-02-15T00:00:00Z"},
            ]
            if memory:
                memory.update_properties(new, {"scope_go_lives": correction})
            else:
                result = backend.update(new, properties={"scope_go_lives": correction})
                assert not result.get("error"), result
            for recorded, expected in [
                (known_before, new),
                (datetime.now(timezone.utc).isoformat(), old),
            ]:
                assert (
                    ast.literal_eval(
                        await call(
                            "decision_get", decision_id=old, as_of=recorded, **at_a
                        )
                    )["decision_id"]
                    == expected
                )
                assert [
                    h["item_id"] for h in await search(as_of_date=recorded, **at_a)
                ] == [expected]
            # G16 omitted date belongs to the resolver, not the tool layer.
            assert (
                ast.literal_eval(
                    await call(
                        "decision_get", decision_id=old, business_scope={"site": "A"}
                    )
                )["decision_id"]
                == new
            )
            # G11 validation before mutation. Errors must never render as not found.
            for schedule in [[], card["versions"][1]["scope_go_lives"] * 2]:
                with pytest.raises(ToolError, match="invalid_scoped_validity"):
                    await call(
                        "decision_supersede",
                        decision_id=old,
                        new_content="invalid",
                        reason="invalid",
                        scope_go_lives=schedule,
                    )
            with pytest.raises(ToolError, match="scoped_validity_conflict"):
                await call(
                    "decision_supersede",
                    decision_id=old,
                    new_content="fork",
                    reason="conflict",
                    scope_go_lives=[
                        {"scope": {"site": "A"}, "valid_from": "2026-04-01T00:00:00Z"}
                    ],
                )
            for opts in [
                {"business_scope": {"country": "US"}},
                {"valid_at": "bad"},
                {"business_scope": {"site": "*"}},
            ]:
                with pytest.raises(ToolError, match="invalid_scoped_validity"):
                    await call("decision_get", decision_id=old, **opts)
            with pytest.raises(ToolError):
                await call(
                    "memory_search",
                    query="",
                    business_scope={"site": "A"},
                    include_superseded=True,
                )
            # G07/G14 real authenticated ownership path for both HTTP modes.
            if not memory:
                for owner in owners[1:]:
                    foreign = RemoteBackend(
                        api_url=url,
                        api_key=owner["access_token"],
                        team_id=owner["workspace_id"],
                    )
                    assert foreign.decision_get(old, **at_a) is None
                    other = foreign.decision_create(
                        "isolated CRF guidance",
                        scope_go_lives=card["versions"][0]["scope_go_lives"],
                    )["decision_id"]
                    assert foreign.decision_get(other, **at_a)["decision_id"] == other
                    assert backend.decision_get(other, **at_a) is None
                denied = type(backend).__new__(type(backend))
                # Invalid credentials exercise real service refusal, preserving the hosted hook.
                if mode == "hosted":
                    from smartmemory_mcp.hosted.exchange import ExchangeCache

                    denied.__init__(
                        url,
                        "invalid",
                        backend._session["team_id"],
                        fingerprint="test_scoped_u12_denied",
                        cache=ExchangeCache(),
                    )
                else:
                    denied.__init__(
                        api_url=url,
                        api_key="invalid",
                        team_id=backend._session["team_id"],
                    )
                with pytest.raises(Exception, match="401|403|session expired"):
                    denied.decision_get(old, **at_a)
            # G08 stays byte-compatible for unqualified legacy decisions.
            legacy = await call("decision_create", content="legacy original")
            legacy_id = legacy.splitlines()[0].split(": ", 1)[1]
            receipt = await call(
                "decision_supersede",
                decision_id=legacy_id,
                new_content="legacy amended",
                reason="global",
            )
            assert "superseded" in receipt and "scheduled" not in receipt
            # G14 hidden successor is refused, never used to synthesize the old rule.
            # It is the last operation. Runtime teardown removes this entire owned
            # store/workspace even on failure, including the hidden record.
            if memory:
                memory.update_properties(new, {"deleted": True})
            else:
                response = backend.update(new, properties={"deleted": True})
                assert not response.get("error"), response
            with pytest.raises(ToolError, match="scoped_history_unavailable"):
                await call("decision_get", decision_id=old, **at_a)

    asyncio.run(run())
