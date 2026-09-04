"""Integration tests for backend dispatch logic.

Tests Task 27 from PLAT-MCP-UNIFY-1 plan:
- Local backend returned when smartmemory package is installed
- Remote backend returned when mode=remote or env var fallback
- RuntimeError when no backend is resolvable
- Backend caching and reset
"""

from unittest.mock import MagicMock, patch

import pytest

from smartmemory_mcp.backends.dispatch import reset_backend, resolve_backend


@pytest.fixture(autouse=True)
def _reset_backend_cache():
    """Ensure each test starts with a clean backend cache."""
    reset_backend()
    yield
    reset_backend()


class TestLocalBackend:
    def test_local_backend_when_smartmemory_installed(self):
        """Default config (mode=local, smartmemory installed) -> LocalBackend."""
        fake_cfg = MagicMock()
        fake_cfg.mode = "local"

        with (
            patch(
                "smartmemory_mcp.backends.dispatch.resolve_backend.__module__",
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {
                    "smartmemory_app": MagicMock(),
                    "smartmemory_app.config": MagicMock(
                        load_config=MagicMock(return_value=fake_cfg)
                    ),
                },
            ),
            patch(
                "smartmemory_mcp.backends.local.LocalBackend.__init__",
                return_value=None,
            ),
        ):
            # Re-import to pick up mocked modules
            from smartmemory_mcp.backends.local import LocalBackend

            backend = resolve_backend()
            assert isinstance(backend, LocalBackend)


class TestRemoteBackend:
    def test_remote_backend_from_env(self):
        """No smartmemory package (ImportError), SMARTMEMORY_API_KEY set -> RemoteBackend."""
        # Make smartmemory_app.config import raise ImportError
        real_import = (
            __builtins__.__import__
            if hasattr(__builtins__, "__import__")
            else __import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "smartmemory_app.config":
                raise ImportError("No smartmemory_app")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=fake_import),
            patch(
                "smartmemory_mcp.tier.get_api_key", return_value="sk_test_key_remote"
            ),
        ):
            backend = resolve_backend()
            from smartmemory_mcp.backends.remote import RemoteBackend

            assert isinstance(backend, RemoteBackend)

    def test_remote_backend_from_config_mode(self):
        """Config mode=remote -> RemoteBackend even when smartmemory is installed."""
        fake_cfg = MagicMock()
        fake_cfg.mode = "remote"
        fake_cfg.api_url = "https://api.test.smartmemory.ai"
        fake_cfg.team_id = "team_123"

        with patch.dict(
            "sys.modules",
            {
                "smartmemory_app": MagicMock(),
                "smartmemory_app.config": MagicMock(
                    load_config=MagicMock(return_value=fake_cfg)
                ),
            },
        ):
            backend = resolve_backend()
            from smartmemory_mcp.backends.remote import RemoteBackend

            assert isinstance(backend, RemoteBackend)


class TestErrorPaths:
    def test_error_when_no_backend(self):
        """No smartmemory package, no env var -> RuntimeError."""
        real_import = (
            __builtins__.__import__
            if hasattr(__builtins__, "__import__")
            else __import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "smartmemory_app.config":
                raise ImportError("No smartmemory_app")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=fake_import),
            patch("smartmemory_mcp.tier.get_api_key", return_value=""),
        ):
            with pytest.raises(RuntimeError, match="No SmartMemory backend available"):
                resolve_backend()


class TestCaching:
    def test_backend_caching(self):
        """resolve_backend() called twice returns the same instance."""
        fake_cfg = MagicMock()
        fake_cfg.mode = "local"

        with (
            patch.dict(
                "sys.modules",
                {
                    "smartmemory_app": MagicMock(),
                    "smartmemory_app.config": MagicMock(
                        load_config=MagicMock(return_value=fake_cfg)
                    ),
                },
            ),
            patch(
                "smartmemory_mcp.backends.local.LocalBackend.__init__",
                return_value=None,
            ),
        ):
            first = resolve_backend()
            second = resolve_backend()
            assert first is second

    def test_reset_backend(self):
        """reset_backend() clears cache; next resolve returns a fresh instance."""
        fake_cfg = MagicMock()
        fake_cfg.mode = "local"

        with (
            patch.dict(
                "sys.modules",
                {
                    "smartmemory_app": MagicMock(),
                    "smartmemory_app.config": MagicMock(
                        load_config=MagicMock(return_value=fake_cfg)
                    ),
                },
            ),
            patch(
                "smartmemory_mcp.backends.local.LocalBackend.__init__",
                return_value=None,
            ),
        ):
            first = resolve_backend()
            reset_backend()
            second = resolve_backend()
            assert first is not second
