"""Liveness route coverage for single-identity HTTP mode."""

from fastmcp import FastMCP
from starlette.testclient import TestClient

from smartmemory_mcp.server import _register_http_health


def test_http_health_is_served_without_authentication() -> None:
    mcp = FastMCP("smartmemory-test")
    _register_http_health(mcp, ["--http"])

    with TestClient(mcp.http_app(json_response=True)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "http"}
