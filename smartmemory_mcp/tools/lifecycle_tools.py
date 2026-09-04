"""Lifecycle tools for DIST-AGENT-HOOKS-1.

Provides memory_auto() — a single FREE-tier tool that enables/disables
the automatic memory lifecycle and configures recall strategy.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from smartmemory_mcp.tools.common import graceful

log = logging.getLogger(__name__)


def register(mcp) -> None:
    """Register lifecycle tools on the MCP server."""

    @mcp.tool()
    @graceful
    def memory_auto(
        session_id: str = "",
        enabled: bool = True,
        recall_strategy: str = "topic_change",
        orient_budget: int = 1500,
        recall_budget: int = 500,
        observe_tool_calls: bool = True,
        distill_turns: bool = True,
        learn_from_errors: bool = True,
    ) -> str:
        """Enable or disable automatic memory lifecycle.

        When enabled, SmartMemory automatically recalls context at session start,
        observes tool calls, distills conversation turns, learns from errors, and
        persists session summaries. Zero manual tool calls needed.

        Session-scoped overrides — settings apply to the current session only,
        not persisted to config.toml.

        Args:
            enabled: Activate (True) or deactivate (False) automatic lifecycle.
            recall_strategy: "session_only" | "topic_change" | "every_prompt".
            orient_budget: Max tokens for session-start context injection.
            recall_budget: Max tokens for per-prompt context injection.
            observe_tool_calls: Capture PostToolUse observations.
            distill_turns: Save (prompt, response) pairs.
            learn_from_errors: Capture PostToolUseFailure errors.

        Returns:
            Current lifecycle configuration summary.
        """
        overrides = {
            "enabled": enabled,
            "recall_strategy": recall_strategy,
            "orient_budget": orient_budget,
            "recall_budget": recall_budget,
            "observe_tool_calls": observe_tool_calls,
            "distill_turns": distill_turns,
            "learn_from_errors": learn_from_errors,
        }

        # Write overrides to session state file so hook-driven CLI calls see them.
        if not session_id:
            log.warning(
                "memory_auto called without session_id — overrides will not persist"
            )
        else:
            _write_session_overrides(session_id, overrides)

        status = "enabled" if enabled else "disabled"
        parts = [
            f"Lifecycle {status}.",
            f"Recall: {recall_strategy}, Orient: {orient_budget}t, Recall: {recall_budget}t.",
        ]
        flags = []
        if observe_tool_calls:
            flags.append("observe")
        if distill_turns:
            flags.append("distill")
        if learn_from_errors:
            flags.append("learn")
        if flags:
            parts.append(f"Active: {', '.join(flags)}.")

        return " ".join(parts)


def _write_session_overrides(session_id: str, overrides: dict) -> None:
    """Write config overrides to a specific session state file."""
    # Sanitize session_id
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")
    if not safe_id:
        return

    data_dir = os.environ.get("SMARTMEMORY_DATA_DIR", str(Path.home() / ".smartmemory"))
    sessions_dir = Path(data_dir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    path = sessions_dir / f"{safe_id}.json"
    try:
        # Read existing state or create new
        if path.exists():
            data = json.loads(path.read_text())
        else:
            data = {"session_id": session_id}
        data["config_overrides"] = overrides
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.rename(path)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to write session overrides: %s", e)
