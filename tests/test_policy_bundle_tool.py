"""GOV-STRATUM-SEAM-1 P1 coverage for the MCP policy-bundle tool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import memory_tools


def _registered() -> dict:
    captured: dict = {}

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def decorator(function):
                captured[function.__name__] = function
                return function

            return decorator

    memory_tools.register_free(FakeMCP())
    return captured


class Backend:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict] = []

    def policy_bundle(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.result


def _bundle() -> dict:
    return {
        "bundle_id": "a" * 64,
        "workspace_id": "workspace-a",
        "compiled_at": "2026-08-21T00:00:00Z",
        "selector": {"status": ["active"]},
        "rules": [],
    }


def test_memory_policy_bundle_is_registered_on_the_free_tier():
    assert "memory_policy_bundle" in _registered()


def test_memory_policy_bundle_returns_backend_json_and_forwards_selectors():
    backend = Backend(_bundle())
    with patch.object(memory_tools, "get_backend", return_value=backend):
        result = _registered()["memory_policy_bundle"](
            workflow="deploy", domain="security"
        )

    assert result == _bundle()
    assert backend.calls == [{"workflow": "deploy", "domain": "security"}]


def test_local_backend_calls_the_core_facade():
    backend = object.__new__(LocalBackend)
    backend._mem = SimpleNamespace(compile_policy_bundle=Mock(return_value=_bundle()))

    assert backend.policy_bundle(workflow="deploy", domain="security") == _bundle()
    backend._mem.compile_policy_bundle.assert_called_once_with(
        workflow="deploy", domain="security"
    )


def test_remote_backend_uses_policy_bundle_get_route():
    backend = object.__new__(RemoteBackend)
    backend._request = Mock(return_value=_bundle())

    assert backend.policy_bundle(workflow="deploy", domain="security") == _bundle()
    backend._request.assert_called_once_with(
        "GET",
        "/memory/policy/bundle",
        params={"workflow": "deploy", "domain": "security"},
    )
