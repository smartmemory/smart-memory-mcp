"""Memory CRUD, search, list, and stats MCP tools."""

import logging
import uuid as _uuid
from typing import Any, Dict, List, Optional

from mcp.types import ToolAnnotations

from smartmemory_mcp.tools.search_window import with_search_window

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


# CORE-MEMORY-DYNAMICS-1 M1a — legacy memory_recall() scope for the standalone MCP.
# Pre-shim memory_recall filtered to memory_type="working" (memory_tools.py:241).
# CORE-MEMORY-DYNAMICS-1 M1b renamed "working" → "pending"; the scope follows.
# Matches the service repo's scope; per plan-m1a.md both repos derive independently.
_LEGACY_RECALL_TYPE_SCOPE: set = {"pending"}

# Module-level one-shot deprecation flag — logs exactly once per process.
_RECALL_DEPRECATION_WARNED: bool = False

# Same idea for the origin-tier filter, which is skipped when `smartmemory` core
# is absent (the hosted wheel does not ship it).
_ORIGIN_FILTER_WARNED: bool = False


def _warn_origin_filter_unavailable(exc: Exception) -> None:
    """Say what was lost when the CORE-ORIGIN-1 tier filter cannot run.

    This filter is the ONLY thing that hides tier-3 speculative-derived items
    from a search: `SearchRequest` carries no origin field, so the service does
    not filter by tier for us. Skipping it silently means the caller sees
    speculative content believing it is user content. Logged once per process.
    """
    global _ORIGIN_FILTER_WARNED
    if _ORIGIN_FILTER_WARNED:
        return
    _ORIGIN_FILTER_WARNED = True
    logger.warning(
        "CORE-ORIGIN-1 search tier filter unavailable (%s); results are NOT "
        "filtered by origin tier, so speculative derived items may be shown "
        "alongside user content. Logged once per process.",
        exc,
    )


_IDENTITY_METADATA_KEYS = frozenset(
    {"tenant_id", "workspace_id", "team_id", "user_id", "run_id"}
)


def _strip_identity_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return an item copy without server-only tenant and execution identity."""
    sanitized = {
        key: value for key, value in item.items() if key not in _IDENTITY_METADATA_KEYS
    }
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        sanitized["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key not in _IDENTITY_METADATA_KEYS
        }
    return sanitized


def _estimate_tokens(text: str) -> int:
    """Cheap per-item token estimate — 1 token ≈ 4 characters."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _build_working_context(
    backend,
    session_id: str,
    query: str,
    k: int,
    max_tokens: Optional[int],
    strategy: Optional[str],
) -> Dict[str, Any]:
    """Build a contract-shaped surfacing response against the backend.

    Standalone MCP has no SmartMemory instance — we compose the response
    from ``backend.search`` directly.  Mirrors
    ``SmartMemory.get_working_context`` from core.  Anchors are NOT
    supported in the standalone (no AnchorQueries).

    CORE-MEMORY-DYNAMICS-1 M2a Slice A: activation scoring now reads the
    nested ``metadata["activation"]`` dict via
    ``smartmemory.activation.score.compute_activation_score``. Items without
    activation metadata fall back to the neutral default (0.5) — same shape
    as core M1a behavior, just routed through the primitive so the score
    decays once Slice B migration seeds items.
    """
    from smartmemory.activation.score import compute_activation_score

    decision_id = _uuid.uuid4().hex
    fetch_k = max(k * 5, k)
    raw = list(backend.search(query, top_k=fetch_k) or [])

    items: List[Dict[str, Any]] = []
    tokens_used = 0
    for row in raw[:k]:
        # ``row`` is a MemoryResult dict returned by backend.search.
        content = row.get("content", "")
        item_tokens = _estimate_tokens(content)
        if max_tokens is not None and tokens_used + item_tokens > max_tokens:
            break
        activation_score = compute_activation_score(row)
        items.append(
            _strip_identity_metadata(
                {
                    "item_id": row.get("item_id"),
                    "content": content,
                    "memory_type": row.get("memory_type"),
                    "metadata": row.get("metadata") or {},
                    "score_breakdown": {
                        "activation": activation_score,
                        "relevance": float(row.get("score") or 0.0),
                        "recency": 1.0,
                        "centrality": 1.0,
                        "anchor_forced": False,
                        "session_pin_boost": 0.0,
                        "freshness_boost": 0.0,
                    },
                }
            )
        )
        tokens_used += item_tokens

    if max_tokens is not None and raw and not items:
        # Smallest mandatory item exceeds budget.
        raise ValueError(
            f"budget_too_small: max_tokens={max_tokens} cannot fit the smallest item"
        )

    return {
        "decision_id": decision_id,
        "items": items,
        "drift_warnings": [],
        "strategy_used": "fast:recency",
        "tokens_used": tokens_used,
        "tokens_budget": max_tokens,
        "deprecation": None,
    }


# ---------------------------------------------------------------------------
# Helpers — backends return normalized dicts (MemoryResult)
# ---------------------------------------------------------------------------


def _relative_age(dt) -> str:
    """Convert a UTC datetime to a human-readable relative age string."""
    if dt is None:
        return ""
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        aware_dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        days = (now - aware_dt).days
        if days <= 0:
            return "today"
        if days == 1:
            return "1d ago"
        if days < 30:
            return f"{days}d ago"
        if days < 365:
            return f"{days // 30}mo ago"
        return f"{days // 365}y ago"
    except Exception:
        return ""


def _format_catalog(query: str, results: list) -> str:
    """Format search results as a compact ranked catalog."""
    if not results:
        return f"No results found for query: {query}"

    lines = [f"Memory catalog for '{query}' — {len(results)} results", "─" * 43]
    for i, item in enumerate(results, 1):
        meta = item["metadata"] or {}

        content = str(item["content"]).replace("\n", " ")
        title = str(meta.get("title", "")).replace("\n", " ")
        snippet_src = title if title else content
        snippet = (snippet_src[:100] + "...") if len(snippet_src) > 100 else snippet_src

        item_id = item["item_id"]
        mtype = item["memory_type"]
        score = item.get("score")
        score_str = f"  {score:.3f}" if score is not None else ""
        age_str = _relative_age(item.get("created_at"))
        stale = meta.get("stale", False)
        stale_str = "  ⚠ stale" if stale else ""

        lines.append(f"{i}. {item_id}  {mtype}{score_str}  {age_str}{stale_str}")
        lines.append(f"   {snippet}")
        lines.append("")

    lines.append("─" * 43)
    lines.append("Use memory_get(item_id) to read any item in full.")
    return "\n".join(lines)


def _format_turn_content(user_turn: str, assistant_turn: str) -> str:
    """Format a user/assistant turn pair for storage."""
    return f"USER: {user_turn.strip()}\nASSISTANT: {assistant_turn.strip()}"


def _format_recall(
    query: str, results: list, session_id: str = None, drift_warnings: list = None
) -> str:
    """Format recalled turns as a prompt-ready context string."""
    if not results:
        if session_id:
            return "No prior turns found for this session."
        return f"No prior turns found for: {query}"

    lines = [f"Relevant prior context for '{query}':", ""]
    for i, item in enumerate(results, 1):
        age_str = _relative_age(item.get("created_at"))
        age_part = f"{age_str} — " if age_str else ""
        content = str(item["content"]).replace("\n", " ↵ ")
        snippet = (content[:300] + "…") if len(content) > 300 else content
        lines.append(f"[{i}] {age_part}{snippet}")
        lines.append("")

    item_ids = ", ".join(str(item["item_id"]) for item in results if item["item_id"])
    lines.append(
        f"(Retrieved {len(results)} turns."
        + (
            f" Use memory_get(item_id) for full content. IDs: {item_ids}"
            if item_ids
            else ""
        )
        + ")"
    )

    if drift_warnings:
        lines.append("")
        lines.append("⚠ Anchor drift warnings:")
        for dw in drift_warnings:
            severity = dw.get("severity", "unknown")
            acontent = str(dw.get("anchor_content", ""))[:100]
            dscore = dw.get("drift_score", 0)
            missing = dw.get("missing_keywords", [])[:5]
            missing_str = ", ".join(missing) if missing else "none"
            lines.append(
                f"  [{severity}] {acontent} (drift={dscore:.2f}, missing: {missing_str})"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FREE tier tools (4)
# ---------------------------------------------------------------------------


def register_free(mcp):
    """Register FREE-tier memory tools (ingest, search, recall, get)."""

    @mcp.tool(
        title="Ingest a memory",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_ingest(content: str, memory_type: str = "semantic") -> str:
        """Ingest content through the full NLP pipeline with entity extraction."""
        backend = get_backend()
        result = backend.ingest(content, memory_type=memory_type)
        if isinstance(result, dict):
            if "error" in result:
                return f"Ingest failed: {result['error']}"
            item_id = result.get("item_id", "unknown")
        else:
            item_id = str(result)
        return f"Memory ingested (pipeline). Item ID: {item_id}"

    @mcp.tool(
        title="Search memories",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    @with_search_window
    def memory_search(
        query: str,
        top_k: int = 5,
        memory_type: Optional[str] = None,
        enable_hybrid: bool = True,
        catalog_mode: bool = True,
        decompose: bool = False,
        channel_weights: Optional[dict] = None,
        multi_hop: bool = False,
        hop_strategy: Optional[str] = None,
        max_hops: int = 3,
        budget_ms: int = 1500,
        cite: bool = False,
        as_of_date: Optional[str] = None,
        as_of_strict: bool = False,
        include_superseded: bool = False,
        include_retracted: bool = False,
        include_archived: bool = False,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ):
        """Search memories using semantic similarity with optional hybrid mode.

        hop_strategy: consensus follows entities several top results agree on; relevance
        follows best-result entities and one-off bridges; semantic asks an LLM.
        since/until: ISO-8601 or relative (7d, 24h, 30m), echoed as an absolute window.

        as_of_date (ISO-8601) travels transaction time: what the system
        believed at that instant. include_superseded keeps replaced items
        visible in results (PLAT-AUDITABLE-MEMORY-1). include_retracted keeps
        WITHDRAWN beliefs visible (CORE-RETRACTED-RECALL-1) — a separate
        lifecycle state with no replacement, not covered by include_superseded.
        include_archived keeps ARCHIVED items visible (CORE-ARCHIVED-RECALL-1) —
        memories the decay/prune evolvers retired, or the source an
        episodic-to-semantic promotion replaced; a third lifecycle state, again
        with no replacement and no version chain. All three default to false, so
        by default you will not be shown a memory the system already knows was
        replaced, withdrawn, or retired.

        Results from an as_of_date search carry as_of_resolution. A value of
        "unresolved" means that result is PRESENT-DAY content the system could
        not resolve to the requested time, so it must not be cited as what was
        believed then. Set as_of_strict to refuse a partial history outright
        rather than receive labelled results (gap #2).
        """
        backend = get_backend()
        # SELF-IMPROVE-6 fix: pass actual top_k, not 3x over-fetch.
        # Over-fetch for origin filtering + reranking happens server-side.
        # The old 3x inflated shown_ids for feedback, diluting selection_rate.
        results = backend.search(
            query,
            top_k=top_k,
            memory_type=memory_type,
            enable_hybrid=enable_hybrid,
            decompose_query=decompose,
            channel_weights=channel_weights,
            multi_hop=multi_hop,
            **({"hop_strategy": hop_strategy} if hop_strategy is not None else {}),
            max_hops=max_hops,
            budget_ms=budget_ms,
            as_of_date=as_of_date,
            as_of_strict=as_of_strict,
            include_superseded=include_superseded,
            include_retracted=include_retracted,
            include_archived=include_archived,
            **{
                k: v
                for k, v in {"since": since, "until": until}.items()
                if v is not None
            },
        )

        # CORE-ORIGIN-1: apply search tier policy
        try:
            from smartmemory.origin_policy import filter_by_tiers, get_default_tiers

            results = filter_by_tiers(results, get_default_tiers("search"))
        except Exception as exc:
            _warn_origin_filter_unavailable(exc)

        results = results[:top_k]

        if not results:
            if cite:
                # RECALL-CITATIONS-1: distinguish "no results" from "no citations requested"
                return {
                    "items": [],
                    "footnote_block": "",
                    "citations": [],
                    "message": f"No results found for query: {query}",
                }
            return f"No results found for query: {query}"

        # RECALL-CITATIONS-1: when cite=True, return structured payload with
        # pre-formatted footnote_block ready for the consuming agent to paste.
        if cite:
            from smartmemory.search.citations import (
                build_citations,
                build_footnote_block,
            )

            citations = [c.to_dict() for c in build_citations(results)]
            footnote_block = build_footnote_block(results)
            return {
                "items": [_strip_identity_metadata(item) for item in results],
                "citations": citations,
                "footnote_block": footnote_block,
            }

        # SELF-IMPROVE-6: capture search_session_id from backend (RemoteBackend stores it
        # on _last_search_session_id after reading the X-Search-Session-Id response header).
        session_id_from_search: str | None = getattr(
            backend, "_last_search_session_id", None
        )

        if catalog_mode:
            catalog = _format_catalog(query, results)
            if session_id_from_search:
                catalog += f"\n\nsearch_session_id: {session_id_from_search}"
            return catalog

        output = [f"Found {len(results)} results for '{query}':\n"]
        for i, item in enumerate(results, 1):
            content = str(item["content"])
            preview = content[:200] + "..." if len(content) > 200 else content
            item_id = item["item_id"]
            mtype = item["memory_type"]
            score = item.get("score")
            score_str = f" (score: {score:.3f})" if score is not None else ""

            # CORE-PROPS-1: confidence display
            confidence = item.get("confidence")
            conf_str = f" [conf: {confidence:.2f}]" if confidence is not None else ""

            # CORE-PROPS-1: stale marker
            stale = item.get("stale", False)
            stale_prefix = "⚠" if stale else ""
            stale_suffix = " [STALE]" if stale else ""

            # CORE-PROPS-1: lineage (derived_from)
            derived = item.get("derived_from", "")
            lineage_str = f" [→{str(derived)[:8]}]" if derived else ""

            # CORE-PROPS-1: low-confidence tilde marker
            id_display = (
                f"~[{item_id}]"
                if confidence is not None and confidence < 0.5
                else f"[{item_id}]"
            )

            output.append(
                f"{i}. {stale_prefix}{id_display} ({mtype}){score_str}{conf_str}{lineage_str}{stale_suffix}"
            )
            output.append(f"   {preview}\n")

        if session_id_from_search:
            output.append(f"\nsearch_session_id: {session_id_from_search}")
        return "\n".join(output)

    @mcp.tool(
        title="Recall memories",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_recall(
        query: str, session_id: Optional[str] = None, top_k: int = 5, cite: bool = False
    ):
        """**Deprecated:** Use ``get_working_context``.

        Legacy surface kept for backward compatibility.  Internally
        delegates to the surfacing helper (CORE-MEMORY-DYNAMICS-1 M1a) and
        post-filters cross-type results through ``_LEGACY_RECALL_TYPE_SCOPE``
        to preserve the pre-shim ``memory_type="working"`` scope.
        """
        global _RECALL_DEPRECATION_WARNED
        if not _RECALL_DEPRECATION_WARNED:
            logger.warning(
                "memory_recall is deprecated; use get_working_context "
                "(CORE-MEMORY-DYNAMICS-1 M1a). Logged once per process."
            )
            _RECALL_DEPRECATION_WARNED = True

        if top_k < 1:
            return "top_k must be at least 1."

        backend = get_backend()
        # Native backend.recall path preserved for LocalBackend's fast path.
        if hasattr(backend, "recall") and not session_id:
            result = backend.recall(cwd=query, top_k=top_k)
            if isinstance(result, str):
                return result

        # Delegate to the surfacing helper, then apply legacy-scope post-filter.
        # Over-fetch aggressively (10x, clamp to contract max 100) so the
        # post-filter has headroom when cross-type retrieval returns few
        # working-typed items in its top-k (Codex review).
        response = _build_working_context(
            backend,
            session_id=session_id or "",
            query=query,
            k=min(max(top_k * 10, top_k), 100),
            max_tokens=None,
            strategy=None,
        )

        filtered: List[dict] = []
        for item in response["items"]:
            mtype = item.get("memory_type")
            anchor_forced = bool(
                (item.get("score_breakdown") or {}).get("anchor_forced")
            )
            if anchor_forced or mtype in _LEGACY_RECALL_TYPE_SCOPE:
                filtered.append(item)

        # Additional session_id filter preserved from pre-shim behavior.
        if session_id:
            filtered = [
                r
                for r in filtered
                if (r.get("metadata") or {}).get("conversation_id", "") == session_id
                or (r.get("metadata") or {}).get("session_id", "") == session_id
            ]

        final = filtered[:top_k]

        # RECALL-CITATIONS-1: structured response with footnote block when cite=True.
        # Empty result set still returns citations=[] (never omitted) so consumers
        # can distinguish "no results" from "no citations requested".
        if cite:
            from smartmemory.search.citations import (
                build_citations,
                build_footnote_block,
            )

            citations = [c.to_dict() for c in build_citations(final)]
            return {
                "items": final,
                "citations": citations,
                "footnote_block": build_footnote_block(final),
                "session_id": session_id,
            }

        return _format_recall(query, final, session_id=session_id)

    @mcp.tool()
    @graceful
    def get_working_context(
        session_id: str,
        query: str,
        k: int = 20,
        max_tokens: Optional[int] = None,
        strategy: Optional[str] = None,
    ) -> dict:
        """Retrieve a structured surfacing response (CORE-MEMORY-DYNAMICS-1 M1a).

        Returns JSON matching
        ``smart-memory-docs/docs/features/CORE-MEMORY-DYNAMICS-1/context-api-contract.json``.
        Cross-type retrieval, ``strategy_used="fast:recency"``.  Standalone
        MCP does not currently compose anchors.
        """
        if k < 1 or k > 100:
            raise ValueError("k must be in 1..100")
        backend = get_backend()
        return _build_working_context(
            backend,
            session_id=session_id,
            query=query,
            k=k,
            max_tokens=max_tokens,
            strategy=strategy,
        )

    @mcp.tool(
        title="Read surrounding conversation",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def read_around(
        item_id: str,
        char_budget: int = 20000,
        before_ratio: float = 0.3,
        after_ratio: float = 0.7,
        cursor: Optional[dict] = None,
    ) -> dict:
        """Auto-centered conversation read (CORE-RECALL-CENTERED-1 Phase 2).

        Given a matched conversation-chunk ``item_id`` (from a recall hit's
        ``conversation_handle``), return a char-budgeted ASYMMETRIC window of the
        surrounding conversation chunks (~``before_ratio`` back / ``after_ratio``
        forward; leftover spills to the other side) plus a continue-cursor — so a
        caller pulls just the relevant slice of a long conversation into context
        instead of the whole transcript. "Centered" is over neighbour 15-turn
        chunks, not individual turns.

        Returns ``{window_items, window_text, handle, continue_cursor:{prev_pos,
        next_pos}, chars_used, char_budget}``. Pass a returned ``continue_cursor``
        back as ``cursor`` to page further in one direction without overlap.
        """
        backend = get_backend()
        return backend.read_around(
            item_id,
            char_budget=char_budget,
            before_ratio=before_ratio,
            after_ratio=after_ratio,
            cursor=cursor,
        )

    @mcp.tool(
        title="Get a memory",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_get(item_id: str) -> str:
        """Retrieve a memory item by ID with full content and metadata."""
        backend = get_backend()
        item = backend.get(item_id)

        if item is None:
            return f"Memory item not found: {item_id}"

        content = item["content"]
        mtype = item["memory_type"]
        meta = item["metadata"]

        parts = [
            f"Memory Item: {item_id}",
            f"Type: {mtype}",
            f"Content: {content}",
        ]
        if meta:
            parts.append(f"Metadata: {meta}")
        return "\n".join(parts)

    @mcp.tool(
        title="Explain a memory",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_explain(memory_id: str):
        """Explain a memory's full provenance: origin, every belief the system
        held for it over time, what replaced it, what it derives from, and
        whether its history is cryptographically intact.

        The single-call audit answer (PLAT-AUDITABLE-MEMORY-1). FREE tier by
        design: agents should always be able to answer "how do you know
        that?". chain_verified null means nothing to verify (legacy or
        unversioned) — it is NOT a tamper warning.
        """
        backend = get_backend()
        result = backend.explain(memory_id)
        if result is None:
            return f"Memory item not found: {memory_id}"
        return result

    # CORE-RECALL-BUDGET-1: known section names, mirrored from
    # smartmemory.recall_pack.KNOWN_SECTIONS. Validated here so a typo fails fast with a
    # usable message instead of travelling to core (local) or over HTTP (remote).
    _RECALL_SECTIONS = (
        "active_plan",
        "anchors",
        "snapshot",
        "tier1_user",
        "tier2_graduated",
        "notes",
        "hot_topics",
        "last_session",
    )

    # Mirrored from smartmemory.recall_pack.PRESETS.
    _RECALL_PRESETS = ("wakeup",)

    @mcp.tool(
        title="Build a recall pack",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_recall_pack(
        budget_tokens: int,
        query: Optional[str] = None,
        sections: Optional[list] = None,
        preset: Optional[str] = None,
    ) -> dict:
        """Assemble the most useful context that fits in a token budget.

        Returns one priority-ordered block (active plan, anchors, latest snapshot,
        tier-1 user content, tier-2 graduated content, notes) packed to fit
        ``budget_tokens``, plus a manifest accounting every token spent and every item
        truncated or dropped (CORE-RECALL-BUDGET-1).

        ``query`` ranks each section by relevance; omit it to rank by recency.
        ``sections`` overrides the default list/order/caps as ``{name, cap_tokens}``
        entries. ``preset`` selects a named section set instead: pass ``"wakeup"`` with a
        small budget (~300 tokens) at session start for an L1 orientation card (active
        plan, anchors, workspace topics, last-session headline) — the default sections go
        degenerate at that size. Use ``get_working_context`` for the L2 drill-in.
        Returns ``{block, manifest}``.
        """
        if not isinstance(budget_tokens, int) or budget_tokens < 1:
            return {"error": "`budget_tokens` must be an integer >= 1."}
        if preset is not None:
            if sections is not None:
                return {"error": "pass either `preset` or `sections`, not both."}
            if preset not in _RECALL_PRESETS:
                return {
                    "error": f"unknown preset {preset!r}; known presets: {', '.join(_RECALL_PRESETS)}"
                }
        for entry in sections or []:
            name = (entry or {}).get("name")
            if name not in _RECALL_SECTIONS:
                return {
                    "error": f"unknown section {name!r}; known sections: {', '.join(_RECALL_SECTIONS)}"
                }

        backend = get_backend()
        return backend.recall_pack(
            budget_tokens=budget_tokens, query=query, sections=sections, preset=preset
        )

    @mcp.tool(
        title="Get policy bundle",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_policy_bundle(
        workflow: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> dict:
        """Compile active workspace decisions into a Stratum policy bundle.

        ``workflow`` is echoed in the selector for the runner. ``domain`` narrows
        the source decisions. The returned JSON is the policy-exchange contract
        bundle and is identical for local and hosted backends.
        """
        return get_backend().policy_bundle(workflow=workflow, domain=domain)


# ---------------------------------------------------------------------------
# PRO tier tools (9)
# ---------------------------------------------------------------------------


def register_pro(mcp):
    """Register PRO-tier memory tools."""

    @mcp.tool(
        title="Add a memory",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_add(
        content: str,
        memory_type: str = "semantic",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Add a memory item directly without the NLP pipeline."""
        backend = get_backend()
        item_metadata = metadata or {}
        if tags:
            item_metadata["tags"] = tags
        item_id = backend.add(content, memory_type=memory_type, metadata=item_metadata)
        return f"Memory added (direct). Item ID: {item_id}"

    @mcp.tool(
        title="Update a memory",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_update(
        item_id: str,
        content: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        properties: Optional[Dict[str, Any]] = None,
        write_mode: Optional[str] = None,
    ) -> str:
        """Update a memory item's properties (CORE-CRUD-UPDATE-1).

        - ``content``/``metadata``: convenience — folded into the properties dict.
        - ``properties``: advanced — direct node-property update. Takes precedence.
        - ``write_mode``: "merge" (default) or "replace".
        """
        backend = get_backend()
        result = backend.update(
            item_id,
            content=content,
            metadata=metadata,
            properties=properties,
            write_mode=write_mode,
        )
        return str(result) if result else f"Updated: {item_id}"

    @mcp.tool(
        title="Delete a memory",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_delete(item_id: str) -> str:
        """Delete a memory item by ID."""
        backend = get_backend()
        success = backend.delete(item_id)
        return f"Deleted: {item_id}" if success else f"Failed to delete: {item_id}"

    @mcp.tool()
    @graceful
    def memory_clear(confirm: bool = False) -> str:
        """Clear all memories permanently (requires confirm=True)."""
        if not confirm:
            return "Safety check: Set confirm=True to actually delete all memories."
        backend = get_backend()
        result = backend.clear_user_memories(confirm=True)
        return str(result)

    @mcp.tool(
        title="List memories",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_list(
        limit: int = 20,
        offset: int = 0,
        metadata_key: str | None = None,
        metadata_value: str | None = None,
    ) -> str:
        """List memories with pagination, optionally filtered by exact metadata match.

        metadata_key/metadata_value must be given together. Nested keys use dot
        syntax (e.g. `profile.tier`). This is the supported replacement for
        memory_search_by_metadata, whose endpoint is deprecated (GRAPH-API-1l).
        """
        backend = get_backend()
        result = backend.list_memories(
            limit=limit,
            offset=offset,
            metadata_key=metadata_key,
            metadata_value=metadata_value,
        )

        # Handle both list and dict responses
        if isinstance(result, dict):
            items = result.get("items", [])
            total = result.get("total", len(items))
        elif isinstance(result, list):
            items = result
            total = len(items)
        else:
            return "Unexpected response format."

        if not items:
            return "No memories found."

        output = [f"Showing {len(items)} of {total} memories:\n"]
        for item in items:
            item_id = item["item_id"]
            content = str(item["content"])
            mtype = item["memory_type"]
            preview = content[:100] + "..." if len(content) > 100 else content
            output.append(f"- [{item_id}] ({mtype}): {preview}")

        return "\n".join(output)

    @mcp.tool(
        title="Memory statistics",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_stats() -> str:
        """Get memory count statistics grouped by type."""
        backend = get_backend()
        try:
            result = backend.stats()
        except NotImplementedError:
            result = backend.get_all_items_debug()

        if isinstance(result, dict):
            total = result.get("total_items", 0)
            by_type = result.get("items_by_type", {})
        else:
            return f"Stats: {result}"

        output = ["Memory Statistics:\n", f"Total memories: {total}", "\nBy type:"]
        for mtype, count in sorted(by_type.items()):
            output.append(f"  - {mtype}: {count}")

        return "\n".join(output)

    @mcp.tool(
        title="Distill a conversation turn",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_distill(
        user_turn: str, assistant_turn: str, session_id: Optional[str] = None
    ) -> str:
        """Ingest a conversation turn pair into pending memory for later recall."""
        backend = get_backend()
        content = _format_turn_content(user_turn, assistant_turn)
        meta: Dict[str, Any] = {"role": "turn", "distill_version": "1"}
        if session_id:
            meta["conversation_id"] = session_id
        item_id = backend.add(content, memory_type="pending", metadata=meta)
        return f"Turn stored: {item_id}"

    @mcp.tool(
        title="Ingest a conversation",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_ingest_conversation(
        turns: list,
        session_boundaries: Optional[list] = None,
        conversation_id: Optional[str] = None,
        session_dates: Optional[list] = None,
        turns_per_chunk: int = 15,
        max_chunk_chars: int = 12000,
    ) -> str:
        """Ingest a conversation as session chunks with full pipeline processing (RLM-1g).

        Groups turns into sessions (by explicit boundaries or auto-chunking),
        runs each chunk through the full extraction pipeline, and creates
        a conversation container node with PART_OF edges.

        Args:
            turns: List of dicts with "role"/"speaker" and "content" keys.
            session_boundaries: Turn indices where sessions start (e.g. [0, 50, 120]).
            conversation_id: User-supplied ID (auto-generated if None).
            session_dates: ISO date per session for metadata.
            turns_per_chunk: Max turns per auto-chunk (default 15).
            max_chunk_chars: Safety split for oversized chunks (default 12000).
        """
        backend = get_backend()
        response = backend.ingest_conversation_sync(
            turns=turns,
            session_boundaries=session_boundaries,
            conversation_id=conversation_id,
            session_dates=session_dates,
            turns_per_chunk=turns_per_chunk,
            max_chunk_chars=max_chunk_chars,
        )
        if isinstance(response, dict):
            ingested = response.get("chunks_ingested", 0)
            failed = response.get("chunks_failed", 0)
            conv_id = response.get("conversation_id", "")
            return (
                f"Conversation {conv_id}: {ingested} chunks ingested, {failed} failed."
            )
        from dataclasses import asdict

        r = asdict(response) if hasattr(response, "__dataclass_fields__") else response
        return f"Conversation ingested: {r}"

    @mcp.tool()
    @graceful
    def memory_search_advanced(
        query: str, algorithm: str = "query_traversal", max_results: int = 15
    ) -> str:
        """Advanced search using Similarity Graph Traversal (SSG) algorithms."""
        try:
            from smartmemory.retrieval.ssg_traversal import SimilarityGraphTraversal
        except ImportError:
            return "Advanced search requires local backend with smartmemory installed."

        backend = get_backend()
        mem = getattr(backend, "_mem", None)
        if mem is None:
            return "Advanced search requires local backend."

        ssg = SimilarityGraphTraversal(mem)
        if algorithm == "query_traversal":
            results = ssg.query_traversal(query, max_results=max_results)
        elif algorithm == "triangulation_fulldim":
            results = ssg.triangulation_fulldim(query, max_results=max_results)
        else:
            return f"Unknown algorithm: {algorithm}. Use 'query_traversal' or 'triangulation_fulldim'"

        if not results:
            return f"No results found for query: {query}"

        output = [f"SSG ({algorithm}) found {len(results)} results for '{query}':\n"]
        for i, item in enumerate(results, 1):
            content = str(item["content"])
            preview = content[:200] + "..." if len(content) > 200 else content
            item_id = item["item_id"]
            mtype = item["memory_type"]
            output.append(f"{i}. [{item_id}] ({mtype})")
            output.append(f"   {preview}\n")

        return "\n".join(output)

    @mcp.tool(
        title="Search memories by metadata",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    @with_search_window
    def memory_search_by_metadata(
        metadata_key: str,
        metadata_value: str,
        top_k: int = 10,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> str:
        """Search memories by exact metadata key-value match."""
        backend = get_backend()
        results = backend.search_by_metadata(
            metadata_key,
            metadata_value,
            top_k=top_k,
            **{
                k: v
                for k, v in {"since": since, "until": until}.items()
                if v is not None
            },
        )

        if not results:
            return f"No memories found with {metadata_key}={metadata_value}"

        output = [
            f"Found {len(results)} memories with {metadata_key}={metadata_value}:\n"
        ]
        for item in results:
            content = str(item["content"])
            preview = content[:150] + "..." if len(content) > 150 else content
            item_id = item["item_id"]
            mtype = item["memory_type"]
            # A superseded memory rendered identically to an active one invites the
            # caller to act on knowledge the system already knows is obsolete.
            if item.get("superseded"):
                replaced_by = item.get("superseded_by")
                marker = f" [SUPERSEDED{f' by {replaced_by}' if replaced_by else ''}]"
            else:
                marker = ""
            output.append(f"- [{item_id}] ({mtype}){marker}: {preview}")

        return "\n".join(output)


# ---------------------------------------------------------------------------
# SELF-IMPROVE-6: Retrieval feedback tool
# ---------------------------------------------------------------------------


def register_feedback(mcp):
    """Register result-selection feedback tool (SELF-IMPROVE-6)."""

    @mcp.tool(
        title="Give search feedback",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_feedback(search_session_id: str, result_used: List[str]) -> str:
        """Report which search results you used from a previous memory_search call.

        Call this after using results from memory_search to help SmartMemory learn
        which memories are actually useful. Pass the search_session_id from the
        memory_search response and the item_ids you incorporated into your response.

        Args:
            search_session_id: The session ID returned at the bottom of the
                memory_search output (e.g. "search:ws123:abc456def789").
            result_used: List of item_ids from the search results that you used.
                Pass an empty list if none of the results were useful.
        """
        backend = get_backend()
        result = backend.submit_feedback(
            search_session_id=search_session_id, result_used=result_used
        )
        if isinstance(result, dict):
            if "error" in result:
                return f"Feedback failed: {result['error']}"
            used = result.get("result_used_count", len(result_used))
            shown = result.get("result_shown_count", "?")
            return f"Feedback recorded: used {used} of {shown} shown results (session: {search_session_id})"
        return f"Feedback recorded for session: {search_session_id}"
