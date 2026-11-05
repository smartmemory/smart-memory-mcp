"""Shared text labels for memory search results."""

from typing import Any


def supersession_label(item: dict[str, Any]) -> str:
    """Label a surfaced successor using either backend marker location."""
    metadata = item.get("metadata")
    previous_id = item.get("via_supersession_of") or (
        metadata.get("via_supersession_of") if isinstance(metadata, dict) else None
    )
    return f" [replaces {str(previous_id)[:8]}]" if previous_id else ""
