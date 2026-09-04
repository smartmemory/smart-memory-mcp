"""Interaction logger for SmartMemory evaluation.

Appends one JSON line per search call to ~/.smartmemory/eval/interactions.jsonl.
Thread-safe, zero dependencies beyond stdlib. Gated by EVAL_LOGGING=true env var.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()

SNIPPET_MAX_LEN = 200


def _eval_data_dir() -> Path:
    """Resolve EVAL_DATA_DIR at call time so env var changes take effect."""
    raw = os.environ.get("EVAL_DATA_DIR", "~/.smartmemory/eval")
    return Path(os.path.expanduser(raw))


def _is_enabled() -> bool:
    return os.environ.get("EVAL_LOGGING", "").lower() in ("true", "1", "yes")


def log_interaction(
    query: str,
    top_k: int,
    memory_type: str | None,
    decompose: bool,
    latency_ms: float,
    raw_results: Any,
) -> None:
    """Append a search interaction to the JSONL log file.

    Args:
        query: The search query text.
        top_k: Requested number of results.
        memory_type: Optional memory type filter.
        decompose: Whether query decomposition was enabled.
        latency_ms: Search latency in milliseconds.
        raw_results: The raw API response (list of dicts or error dict).
    """
    if not _is_enabled():
        return

    # Parse results into compact format
    results_summary: list[dict[str, Any]] = []
    result_count = 0

    if isinstance(raw_results, list):
        result_count = len(raw_results)
        for rank, item in enumerate(raw_results, 1):
            content = item.get("content", "")
            snippet = (
                content[:SNIPPET_MAX_LEN] + "..."
                if len(content) > SNIPPET_MAX_LEN
                else content
            )
            results_summary.append(
                {
                    "item_id": item.get("item_id", ""),
                    "rank": rank,
                    "score": item.get("score"),
                    "memory_type": item.get("memory_type", ""),
                    "content_snippet": snippet,
                }
            )

    record = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "top_k": top_k,
        "memory_type": memory_type,
        "decompose": decompose,
        "result_count": result_count,
        "latency_ms": round(latency_ms, 1),
        "results": results_summary,
    }

    line = json.dumps(record, ensure_ascii=False) + "\n"

    data_dir = _eval_data_dir()
    interactions_file = data_dir / "interactions.jsonl"

    with _lock:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(interactions_file, "a", encoding="utf-8") as f:
            f.write(line)
