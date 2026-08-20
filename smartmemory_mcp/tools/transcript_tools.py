"""DIST-CC-INGEST-1 Phase 4 — search your own past Claude Code / Codex sessions.

The read half of the transcript-ingest feature: `transcripts index` (smart-memory-core
CLI) walks ``~/.claude`` and ``~/.codex``, ingests the conversation prose, and these
tools query what it stored — "have I hit this error before?", "how did I decide X?".

**Why this does not go through `get_backend()`.** The ingested corpus lives in its own
store (``~/.smartmemory-transcripts`` by default), deliberately kept apart from the
curated store the other tools use: 79k conversation turns would swamp every
`memory_search` result. `resolve_backend()`'s singleton is pinned to the *main* data
dir, so it structurally cannot see transcripts. These tools therefore open a second,
read-only SmartMemory instance against the transcript dir. Note this is a per-directory
instance, NOT a process-wide `SMARTMEMORY_DATA_DIR` override — that would silently
repoint every other tool's backend as well.

Distinct from `code_read_transcript` (CORE-CODE-PROVENANCE-1), which reads raw JSONL
*anchored on a code span* — you have a line of code and want the conversation that wrote
it. This is the unanchored direction: semantic search across the whole corpus, which
needs the embeddings only ingestion produces.

Local-only, like `code_read_transcript`: the transcripts are on the developer's machine
and are never uploaded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from smartmemory_mcp.tools.common import graceful

logger = logging.getLogger(__name__)

DEFAULT_TRANSCRIPT_DIR = "~/.smartmemory-transcripts"

# origin prefixes written by `iter_transcript_ingest_kwargs` (core importers).
# `import:` is already tier 1 in origin_policy — visible to both recall and search —
# so no origin-policy change was needed to make these findable.
_SOURCE_ORIGINS = {
    "claude-code": "import:claude_code",
    "claude_code": "import:claude_code",
    "codex": "import:codex",
    "all": "import:",
}

_memory: Any = None
_memory_dir: Path | None = None


def transcript_data_dir() -> Path:
    """Resolve the transcript store directory (env override, then default)."""
    raw = os.environ.get("SMARTMEMORY_TRANSCRIPTS_DIR") or DEFAULT_TRANSCRIPT_DIR
    return Path(raw).expanduser()


def _store_exists(data_dir: Path) -> bool:
    """True when an ingest has actually created a store here.

    Checked on the db file rather than the directory: `create_lite_memory` *creates*
    the directory on open, so a directory test would report a store that holds nothing
    and turn "you haven't run the import" into an empty result set.
    """
    return (data_dir / "memory.db").exists()


def _index_dimension(data_dir: Path) -> int | None:
    """Vector width baked into the on-disk usearch index, or None if unreadable."""
    index_path = data_dir / "memory.usearch"
    if not index_path.exists():
        return None
    try:
        from usearch.index import Index

        meta = Index.metadata(str(index_path)) or {}
    except Exception:  # unreadable metadata must not break search
        return None
    for key in ("dimensions", "ndim", "dimension"):
        if meta.get(key):
            return int(meta[key])
    return None


def _query_dimension() -> int | None:
    """Vector width the CURRENTLY configured embedder produces, or None."""
    try:
        from smartmemory.plugins.embedding import create_embeddings

        probe = create_embeddings("dimension probe")
        return len(probe) if probe is not None else None
    except Exception:
        return None


def _embedder_mismatch(data_dir: Path) -> str | None:
    """Return a diagnostic when the query embedder cannot read this index.

    This is the failure this feature is most exposed to and the one that looks least
    like a failure. `create_lite_memory` does not pin the embedding provider — the core
    CLI calls it bare — so the corpus is embedded with whatever provider was ambient at
    ingest time (locally, 384-dim MiniLM). If `OPENAI_API_KEY` appears in the
    environment afterwards, the provider silently switches, queries embed at a different
    width, and semantic search returns *nothing* with no error raised. Per
    `no-silent-degradation.md`, say so instead of returning an empty list.
    """
    stored = _index_dimension(data_dir)
    current = _query_dimension()
    if stored is None or current is None or stored == current:
        return None
    return (
        f"Embedding mismatch: the transcript index was built at {stored} dimensions, "
        f"but the currently configured embedder produces {current}. Semantic search "
        f"would return nothing rather than fail.\n"
        f"The provider is read from the environment at open time and is NOT pinned, so "
        f"this usually means a provider key (e.g. OPENAI_API_KEY) is set now that was "
        f"absent during ingest, or vice versa.\n"
        f"Fix: restore the embedding provider used for the import, or re-run "
        f"`transcripts index` to rebuild the index at {current} dimensions."
    )


def _not_ingested_message(data_dir: Path) -> str:
    """The honest answer when the corpus has not been imported yet."""
    return (
        f"No transcript store at {data_dir}.\n\n"
        f"Past sessions become searchable only after they are imported. The import is "
        f"long-running (~22h for a full corpus) and must be detached:\n\n"
        f"  smartmemory-core --data-dir {data_dir} transcripts index --detach\n"
        f"  smartmemory-core --data-dir {data_dir} transcripts status --watch\n\n"
        f"Narrower starting points:\n"
        f"  transcripts index --detach --source claude-code   # Claude Code only\n"
        f"  transcripts index --dry-run                       # ~20s, writes nothing\n\n"
        f"Set SMARTMEMORY_TRANSCRIPTS_DIR to point these tools at a different store."
    )


def _get_memory() -> Any:
    """Open (once) a read-only SmartMemory over the transcript store.

    `PipelineConfig.lite()` is passed even though nothing here writes: `create_lite_memory`
    otherwise builds `.default()`, whose `ground` stage carries a Wikidata SPARQL client.
    Search never runs the pipeline, so this only keeps construction cheap — but it also
    means no write path can be added to these tools without reconsidering that cost.
    """
    global _memory, _memory_dir
    data_dir = transcript_data_dir()
    if _memory is not None and _memory_dir == data_dir:
        return _memory

    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    _memory = create_lite_memory(str(data_dir), pipeline_profile=PipelineConfig.lite())
    _memory_dir = data_dir
    return _memory


def reset_memory() -> None:
    """Drop the cached transcript memory (tests, and after a dir change)."""
    global _memory, _memory_dir
    _memory = None
    _memory_dir = None


def _hit_lines(idx: int, item: Any) -> list[str]:
    """Render one search hit. Items arrive as MemoryItem, not dict."""
    meta = getattr(item, "metadata", None) or {}
    origin = getattr(item, "origin", None) or meta.get("origin") or "?"
    source = origin.split(":", 1)[1] if ":" in origin else origin
    title = meta.get("title") or "(untitled session)"
    when = getattr(item, "reference_time", None) or meta.get("session_date") or ""
    content = (getattr(item, "content", None) or "").strip().replace("\n", " ")
    if len(content) > 500:
        content = content[:500] + "…"
    head = f"{idx}. [{source}] {title}"
    if when:
        head += f"  ({when})"
    return [head, f"   {content}", f"   item_id: {getattr(item, 'item_id', '?')}"]


def register(mcp):
    """Register transcript tools (PRO tier, local backend only)."""

    @mcp.tool()
    @graceful
    def transcript_search(query: str, top_k: int = 5, source: str = "all") -> str:
        """Search your own past Claude Code and Codex sessions by meaning
        (DIST-CC-INGEST-1 Phase 4). Use it to recover prior context — "have I hit this
        error before?", "why did we choose X?", "what did I try last time?" — instead of
        re-deriving it.

        Searches only sessions already imported by `transcripts index`; it reports when
        nothing has been imported rather than returning an empty result. Local-only.

        Args:
            query: What to look for, in natural language.
            top_k: Number of sessions to return (default 5).
            source: "claude-code", "codex", or "all" (default).

        Note there is no project filter: the session's working directory is used at
        import time to select files and is not stored on the items, so it cannot be
        filtered on afterwards. Put the project name in the query instead.
        """
        if not query.strip():
            return "Error: `query` is required."
        origin = _SOURCE_ORIGINS.get(source)
        if origin is None:
            return (
                f"Error: unknown source {source!r} "
                f"(expected 'claude-code', 'codex', or 'all')."
            )

        data_dir = transcript_data_dir()
        if not _store_exists(data_dir):
            return _not_ingested_message(data_dir)

        mismatch = _embedder_mismatch(data_dir)
        if mismatch:
            return mismatch

        memory = _get_memory()
        results = memory.search(query, top_k=top_k, origin=origin) or []
        if not results:
            return (
                f"No matching sessions for {query!r} (source={source}).\n"
                f"Store: {data_dir}. Run `transcript_status()` to see how much of the "
                f"corpus has actually been imported — a partial import is the usual "
                f"reason a real memory is missing."
            )

        lines = [f"{len(results)} session(s) matching {query!r} (source={source}):", ""]
        for i, item in enumerate(results, 1):
            lines.extend(_hit_lines(i, item))
            lines.append("")
        return "\n".join(lines).rstrip()

    @mcp.tool()
    @graceful
    def transcript_status() -> str:
        """Report how much of your Claude Code / Codex history is actually searchable
        (DIST-CC-INGEST-1 Phase 4), and whether an import is running right now.

        Call this when `transcript_search` finds nothing you expected: the usual cause
        is that the long-running import has not finished, not that the memory is absent.
        """
        from smartmemory.importers.ledger import LEDGER_FILENAME, TranscriptLedger
        from smartmemory.importers.run_state import (
            RUN_STATE_FILENAME,
            RunStateFile,
            human_duration,
        )

        data_dir = transcript_data_dir()
        if not _store_exists(data_dir):
            return _not_ingested_message(data_dir)

        lines = [f"Transcript store: {data_dir}"]

        mismatch = _embedder_mismatch(data_dir)
        if mismatch:
            lines += ["", mismatch, ""]

        ledger = TranscriptLedger(data_dir / LEDGER_FILENAME).load()
        summary = ledger.summary()
        by_status = summary.get("by_status") or {}
        lines.append(f"Turns ingested: {summary.get('turns_ingested', 0):,}")
        if by_status:
            parts = ", ".join(f"{k}={v:,}" for k, v in sorted(by_status.items()))
            lines.append(f"Files: {parts}")

        failures = ledger.failures()
        if failures:
            lines.append(
                f"Failed files: {len(failures)} (retry: `transcripts index --retry-failed`)"
            )
        interrupted = ledger.interrupted()
        if interrupted:
            # Named rather than hidden: these were killed mid-ingest and will be
            # ingested again, which duplicates them (Lite has no graph-side dedupe).
            lines.append(
                f"Interrupted mid-ingest: {len(interrupted)} — these will re-ingest and may duplicate."
            )

        state = RunStateFile(data_dir / RUN_STATE_FILENAME).read()
        if state is not None and state.is_alive():
            lines.append(
                f"An import is RUNNING (pid {state.pid}, {human_duration(state.elapsed())} elapsed). "
                f"Results will keep growing; re-run this to check progress."
            )
        elif state is not None:
            lines.append("Last import is no longer running (finished or stopped).")
        else:
            lines.append("No import currently running.")

        return "\n".join(lines)
