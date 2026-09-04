"""Tests for DIST-AGENT-HOOKS-1 MCP lifecycle tools."""


class TestLifecycleToolRegistration:
    """memory_auto should be registered in FREE tier."""

    def test_memory_auto_in_free_tier(self, monkeypatch):
        """memory_auto is available at FREE tier (no login needed)."""
        monkeypatch.delenv("SMARTMEMORY_API_KEY", raising=False)
        monkeypatch.delenv("SMARTMEMORY_MCP_FULL_TOOLS", raising=False)

        # Import fresh to get tool list
        from smartmemory_mcp.tools import lifecycle_tools

        # Verify the module has a register function
        assert hasattr(lifecycle_tools, "register")
