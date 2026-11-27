"""Presentation of the decision effects counts supplied by the backend."""

import logging
from collections.abc import Mapping

from fastmcp.tools import ToolResult

from smartmemory_mcp.backends.interface import MemoryBackend

logger = logging.getLogger(__name__)


def counts_summary(counts: Mapping[str, int]) -> str:
    conflicts = counts["conflicts"]
    noun = "conflict" if conflicts == 1 else "conflicts"
    return f"{conflicts} {noun} · {counts['replaced']} replaced · {counts['to_review']} to review"


def write_reply(text: str, backend: MemoryBackend, decision_id: str) -> ToolResult:
    """Attach standing counts without turning a completed write into a retryable tool error."""
    try:
        counts = backend.decision_effects(decision_id)["counts"]
        fields = {"counts": counts, "summary": counts_summary(counts)}
    except Exception:
        logger.warning("Could not read decision effects after write", exc_info=True)
        fields = {
            "counts": None,
            "summary": "Effects unavailable. Retry decision_effects for the saved decision.",
        }
    return ToolResult(content=text, structured_content=fields)
