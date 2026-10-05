"""Local store-free Python effects tool."""

from __future__ import annotations

import logging

from mcp.types import ToolAnnotations

from .common import graceful


def register(mcp):
    """Register the local scanner. Hosted registration replaces this callable."""

    @mcp.tool(
        title="Scan Python effects",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def code_effects(directory: str, repo_name: str | None = None) -> dict:
        """Scan a local Python checkout without opening a memory store."""
        from pathlib import Path

        try:
            from smartmemory.code.effects import scan_effects
        except ImportError:
            logging.getLogger(__name__).warning(
                "SmartMemory unavailable, Python effects evidence was not obtained"
            )
            raise

        root = Path(directory).resolve()
        return scan_effects(root, repo_name or root.name)
