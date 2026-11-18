"""Shared text labels for memory search results."""

from typing import Any


def supersession_label(item: Any) -> str:
    """Label a surfaced successor using either backend marker location.

    Accepts a result dict or a MemoryItem-like object (2.0 SSG returns MemoryItems).
    """
    if isinstance(item, dict):
        metadata = item.get("metadata")
        top = item.get("via_supersession_of")
    else:
        metadata = getattr(item, "metadata", None)
        top = getattr(item, "via_supersession_of", None)
    previous_id = top or (
        metadata.get("via_supersession_of") if isinstance(metadata, dict) else None
    )
    return f" [replaces {str(previous_id)[:8]}]" if previous_id else ""
