"""Decision effects on the MCP wire. draft-a.json records the A4s preview handler at b84a9685
with A3's frozen 15-note Lite store and offline model replies. The capture injected that store
at the handler's memory factory. Scope and quota enforcement belong to the service suite.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import decision_tools

RECORDING = Path(__file__).parent / "fixtures" / "decision-effects-draft-a.json"


@pytest.fixture
def wire(monkeypatch):
    """Run the actual MCP tool, backend and HTTP serialization against recorded responses."""
    backend = RemoteBackend(
        api_url="https://effects.invalid", api_key="fixture", team_id="effects-a8"
    )
    backend._session["_bootstrapped"] = True
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    server = FastMCP("effects-a8")
    decision_tools.register(server)
    requests = []

    def call(tool, args, replies):
        def handle(request):
            assert request.headers["X-Workspace-Id"] == "effects-a8"
            assert request.headers["Authorization"] == "Bearer fixture"
            requests.append(
                (
                    request.method,
                    request.url.path,
                    json.loads(request.content) if request.content else None,
                )
            )
            status, body = replies[len(requests) - 1]
            return httpx.Response(status, json=body)

        async def run():
            async with Client(server) as client:
                return await client.call_tool(tool, args, raise_on_error=False)

        with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
            monkeypatch.setattr(httpx, "request", transport.request)
            return asyncio.run(run())

    return call, requests


def test_frozen_draft_a_matches_the_route(wire):
    call, requests = wire
    recording = json.loads(RECORDING.read_text())
    text, snapshot, ids = (
        recording["text"],
        recording["reply"],
        recording["decision_ids"],
    )
    result = call("decision_effects", {"text": text}, [(200, snapshot)])
    assert requests == [("POST", "/memory/decisions/effects/preview", {"text": text})]
    assert not result.is_error
    assert [block.text for block in result.content] == [
        "1 conflict · 1 replaced · 3 to review"
    ]
    data = result.structured_content
    assert (
        data == snapshot
    )  # Full lists, graph, dates, reasons and suggestions in route order.
    assert data["replaces"] == [ids["2"]]
    assert [row["id"] for row in data["conflicts"]] == [ids["1"]]
    assert "EU" in data["conflicts"][0]["why"]
    assert [(row["id"], row["hop"], row["via"]) for row in data["review"]] == [
        (ids["3"], 1, ids["2"]),
        (ids["4"], 1, ids["2"]),
        (ids["5"], 2, ids["3"]),
    ]
    assert data["counts"] == {"conflicts": 1, "replaced": 1, "to_review": 3}


@pytest.mark.parametrize("selector", ["saved", "draft"])
def test_suggested_links_survive_both_read_paths(wire, selector):
    call, requests = wire
    recording = json.loads(RECORDING.read_text())
    snapshot = recording["reply"]
    edge = next(
        edge for edge in snapshot["data"]["edges"] if edge["type"] == "depends_on"
    )
    edge["suggested"] = True
    node = next(
        node for node in snapshot["data"]["nodes"] if node["id"] == edge["source"]
    )
    node["viaSuggested"] = True
    snapshot["suggestedLinks"] = "dashed"
    snapshot["suggestions"] = [
        {"source": edge["source"], "target": edge["target"], "reason": "uses the rule"}
    ]
    if selector == "saved":
        snapshot.update(phase="standing", focus=node["id"])
        args = {"decision_id": node["id"]}
        expected_request = ("GET", f"/memory/decisions/{node['id']}/effects", None)
    else:
        args = {"text": recording["text"]}
        expected_request = ("POST", "/memory/decisions/effects/preview", args)

    result = call("decision_effects", args, [(200, snapshot)])
    assert requests == [expected_request]
    assert result.structured_content == snapshot


@pytest.mark.parametrize(
    "args",
    [{}, {"decision_id": "a", "text": "draft"}, {"text": " "}, {"decision_id": ""}],
)
def test_invalid_selector_makes_no_request(wire, args):
    call, requests = wire
    assert call("decision_effects", args, []).is_error
    assert requests == []


@pytest.mark.parametrize(
    ("args", "status"), [({"decision_id": "missing"}, 404), ({"text": "draft"}, 429)]
)
def test_read_failure_is_an_error_without_counts(wire, args, status):
    call, requests = wire
    result = call("decision_effects", args, [(status, {"detail": "unavailable"})])
    assert result.is_error and result.structured_content is None
    assert str(status) in result.content[0].text
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("tool", "args", "path", "response", "focus", "text"),
    [
        (
            "decision_create",
            {"content": "Use EU"},
            "create",
            {"decision_id": "a", "decision_type": "policy", "confidence": 0.8},
            "a",
            "Decision created: a\nType: policy\nConfidence: 0.8",
        ),
        (
            "decision_supersede",
            {"decision_id": "a", "new_content": "Use US", "reason": "cost"},
            "a/supersede",
            {"new_decision_id": "b"},
            "b",
            "Decision a superseded.\nNew decision: b",
        ),
        (
            "decision_retract",
            {"decision_id": "a", "reason": "obsolete"},
            "a/retract",
            {"decision_id": "a", "status": "retracted"},
            "a",
            "Decision retracted: a",
        ),
    ],
)
def test_write_text_and_standing_counts(wire, tool, args, path, response, focus, text):
    call, requests = wire
    counts = {"conflicts": 2, "replaced": 0, "to_review": 5}
    result = call(tool, args, [(200, response), (200, {"counts": counts})])
    assert not result.is_error
    assert [(method, url) for method, url, _ in requests] == [
        ("POST", "/memory/decisions/" + path),
        ("GET", f"/memory/decisions/{focus}/effects"),
    ]
    assert [item.text for item in result.content] == [text]
    assert result.structured_content == {
        "counts": counts,
        "summary": "2 conflicts · 0 replaced · 5 to review",
    }


def test_failed_followup_preserves_write_and_never_repeats_it(wire):
    call, requests = wire
    result = call(
        "decision_retract",
        {"decision_id": "a", "reason": "obsolete"},
        [(200, {"decision_id": "a"}), (503, {"detail": "unavailable"})],
    )
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/memory/decisions/a/retract"),
        ("GET", "/memory/decisions/a/effects"),
    ]
    assert not result.is_error
    assert result.content[0].text == "Decision retracted: a"
    assert result.structured_content["counts"] is None
    assert "Effects unavailable" in result.structured_content["summary"]


def test_retraction_counts_a_real_dependent_on_lite(tmp_path, monkeypatch):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory
    from smartmemory_mcp.backends.local import LocalBackend

    with create_lite_memory(
        str(tmp_path),
        pipeline_profile=PipelineConfig.lite_hermetic(),
        spawn_worker=False,
    ) as memory:
        backend = LocalBackend()
        backend._mem = memory
        monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
        server = FastMCP("effects-a8-lite")
        decision_tools.register(server)
        target = memory.add_decision("Use the Frankfurt vendor").decision_id
        dependent = memory.add_decision("Keep templates with the vendor").decision_id
        memory.add_decision_depends_on(dependent, target)

        async def run():
            async with Client(server) as client:
                write = await client.call_tool(
                    "decision_retract", {"decision_id": target, "reason": "retired"}
                )
                read = await client.call_tool(
                    "decision_effects", {"decision_id": target}
                )
                return write, read

        write, read = asyncio.run(run())
        assert write.content[0].text == f"Decision retracted: {target}"
        assert (
            write.structured_content["summary"]
            == read.content[0].text
            == "0 conflicts · 0 replaced · 1 to review"
        )
        assert write.structured_content["counts"] == read.structured_content["counts"]
        assert [
            (row["id"], row["via"], row["hop"])
            for row in read.structured_content["review"]
        ] == [(dependent, target, 1)]
