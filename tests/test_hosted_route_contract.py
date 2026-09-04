"""Every hosted tool's HTTP route exists in the service's OpenAPI schema (S5).

`RemoteBackend.update()` sent PUT for a route the service only serves as PATCH,
and every core test stayed green for months (design.md §4, finding 10). This
maps each hosted tool to the (method, path) its backend method actually uses and
checks that pair against `smart-memory-service`'s committed OpenAPI snapshot, so
that class of drift fails here instead of at runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from smartmemory_mcp.hosted.tools import HOSTED_TOOLS

SERVICE_REPO = Path(__file__).resolve().parents[2] / "smart-memory-service"
SNAPSHOT = SERVICE_REPO / "tests" / "contracts" / "snapshots" / "openapi_schema.json"

# The route each hosted tool reaches, read off the backend method it calls.
# `None` means the tool makes no svc-api call of its own.
TOOL_ROUTES: dict[str, tuple[str, str] | None] = {
    "memory_ingest": ("post", "/memory/ingest"),
    "memory_search": ("post", "/memory/search"),
    # The hosted variant always takes RemoteBackend.recall, which is search.
    "memory_recall": ("post", "/memory/search"),
    "read_around": ("post", "/memory/read-around"),
    "memory_get": ("get", "/memory/{item_id}"),
    "memory_explain": ("get", "/memory/{memory_id}/explain"),
    "memory_recall_pack": ("post", "/memory/recall/pack"),
    "memory_policy_bundle": ("get", "/memory/policy/bundle"),
    "memory_add": ("post", "/memory/add"),
    "memory_update": ("patch", "/memory/{item_id}"),
    "memory_delete": ("delete", "/memory/{item_id}"),
    "memory_list": ("get", "/memory/list"),
    "memory_stats": ("get", "/memory/health"),
    "memory_distill": ("post", "/memory/add"),
    "memory_ingest_conversation": ("post", "/memory/ingest/conversation"),
    "memory_search_by_metadata": ("get", "/memory/by-metadata"),
    "memory_feedback": ("post", "/memory/result-feedback"),
    "code_search": ("get", "/memory/code/search"),
    "code_dead_code": ("get", "/memory/code/dead-code"),
    "code_dependencies": ("get", "/memory/code/dependencies"),
    "agent_set_recall_profile": ("put", "/memory/agents/{agent_id}/recall-profile"),
    "agent_get_recall_profile": ("get", "/memory/agents/{agent_id}/recall-profile"),
    "reasoning_query_traces": ("post", "/memory/search"),
    "switch_team": ("get", "/memory/teams"),
    "whoami": None,  # session state only
}

_PARAMETER = re.compile(r"\{[^}]*\}")


def _shape(path: str) -> str:
    """Normalise path-parameter NAMES away.

    The service spells the id `{item_id}` where the MCP client calls it
    `{memory_id}`; on the wire they are the same route. Only the shape and the
    literal segments are contractual.
    """
    return _PARAMETER.sub("{}", path)


def _load_snapshot() -> dict:
    if not SERVICE_REPO.is_dir():
        pytest.skip(
            f"smart-memory-service is not checked out at {SERVICE_REPO}; the hosted "
            "route contract cannot be verified in this working copy."
        )
    # The directory IS here, so a missing or broken snapshot is a real failure,
    # never a skip: skipping is how this drift went unnoticed the first time.
    assert SNAPSHOT.is_file(), (
        f"{SERVICE_REPO.name} is checked out but its OpenAPI snapshot is missing at "
        f"{SNAPSHOT}. Regenerate it (smart-memory-service contract tests)."
    )
    try:
        return json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError) as exc:  # pragma: no cover - corrupt snapshot
        raise AssertionError(f"could not read {SNAPSHOT}: {exc}") from exc


def _served_routes() -> set[tuple[str, str]]:
    paths = _load_snapshot().get("paths") or {}
    return {
        (method.lower(), _shape(path))
        for path, operations in paths.items()
        for method in operations
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


def test_the_route_map_covers_every_hosted_tool() -> None:
    """No hosted tool may be missing from the map — that is how one escapes."""
    assert set(TOOL_ROUTES) == set(HOSTED_TOOLS)


@pytest.mark.parametrize(
    ("tool", "route"),
    sorted((name, route) for name, route in TOOL_ROUTES.items() if route is not None),
)
def test_every_hosted_tool_route_is_served(tool: str, route: tuple[str, str]) -> None:
    method, path = route
    served = _served_routes()

    assert (method, _shape(path)) in served, (
        f"hosted tool {tool!r} calls {method.upper()} {path}, which the "
        f"smart-memory-service OpenAPI snapshot does not serve"
    )


def test_the_update_method_regression_stays_fixed() -> None:
    """The original drift: PUT /memory/{id} was sent, only PATCH is served."""
    served = _served_routes()

    assert ("patch", "/memory/{}") in served
    assert ("put", "/memory/{}") not in served
    assert TOOL_ROUTES["memory_update"] == ("patch", "/memory/{item_id}")


def test_the_snapshot_is_readable_and_populated() -> None:
    paths = _load_snapshot().get("paths") or {}

    assert len(paths) > 100, "the OpenAPI snapshot looks truncated"
