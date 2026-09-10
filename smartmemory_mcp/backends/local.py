"""Local backend — delegates to smartmemory_app.storage (optional dependency)."""

from __future__ import annotations

import logging
from typing import Any

from .interface import BackendCapabilities
from .models import MemoryResult, normalize_item, normalize_items

log = logging.getLogger(__name__)


def _metadata_matches(metadata: dict, key: str, value: Any) -> bool:
    """Exact metadata match, honouring the service's dotted-path syntax.

    Mirrors `/memory/list`'s `metadata_key` contract (`profile.tier` descends
    into nested dicts). The value always arrives as a string from an MCP tool
    argument, so comparison is string-based — which keeps bool and int distinct
    for free: `str(True) == "True"` never equals `str(1) == "1"`, so `flag=1`
    cannot match `flag=true`. That is the same bool/int divergence guarded on
    the service side, where Python's `True == 1` disagrees with type-aware
    FalkorDB. Only bools are case-folded, so `"true"` matches stored `True`.
    """
    cur: Any = metadata
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    if isinstance(cur, bool):
        return str(cur).lower() == str(value).strip().lower()
    return str(cur) == str(value)


def _decision_dict(decision: Any) -> dict[str, Any]:
    """Serialize a core Decision to the same dict the REST routes return.

    Tolerates something that is already a dict so a caller-supplied double is
    not forced to re-implement `to_dict()`.
    """
    if isinstance(decision, dict):
        return decision
    return decision.to_dict()


class LocalBackend(BackendCapabilities):
    """Wraps smartmemory package for local-mode operations."""

    # Tools using REST have a separate local implementation.
    unsupported_capabilities = frozenset({"request"})

    def __init__(self) -> None:
        try:
            from smartmemory_app.storage import get_memory

            self._get_memory = get_memory
            self._mem = get_memory()
        except ImportError:
            raise RuntimeError(
                "Local backend requires the smartmemory package.\nInstall with: pip install smartmemory"
            )

    # -- Core CRUD --

    def export_okf(self, bundle_path: str) -> int:
        """Export the active local workspace to an OKF bundle directory."""
        from smartmemory.corpus.exporter import CorpusExporter

        return CorpusExporter(self._mem).run(bundle_path)

    def import_okf(self, bundle_path: str) -> Any:
        """Losslessly import an OKF bundle through the direct add path."""
        from smartmemory.corpus.importer import CorpusImporter

        return CorpusImporter(self._mem, mode="direct").run(bundle_path)

    def add(
        self,
        content: str,
        memory_type: str = "semantic",
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> str:
        """Store a memory item."""
        from smartmemory.models.memory_item import MemoryItem

        # DIST-LITE-QUIET-1: attribute the write so it lands as tier-2 user content, not
        # origin='unknown' (tier 4, hidden from recall+search). An explicit origin in
        # metadata (e.g. an importer) wins; default to this producer.
        meta = dict(metadata or {})
        context = dict(kwargs.pop("context", None) or {})
        meta.update({key: value for key, value in context.items() if key != "origin"})
        from smartmemory import resolve_origin

        origin = resolve_origin(
            context,
            origin=kwargs.pop("origin", None) or meta.pop("origin", None),
            default="mcp:memory_add",
        )
        item = MemoryItem(
            content=content, memory_type=memory_type, metadata=meta, origin=origin
        )
        return self._mem.add(item)

    def get(self, item_id: str, **kwargs: Any) -> MemoryResult | None:
        """Retrieve a memory by ID."""
        result = self._mem.get(item_id)
        if result is None:
            return None
        return normalize_item(result)

    def explain(self, memory_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """Full provenance answer via the core facade (PLAT-AUDITABLE-MEMORY-1)."""
        return self._mem.explain(memory_id)

    def update(
        self,
        item_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        properties: dict | None = None,
        write_mode: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Update an existing memory via the canonical update_properties path.

        See CORE-CRUD-UPDATE-1 contract: `properties` takes precedence over the
        content/metadata conveniences; `write_mode` is "merge" (default) or
        "replace".
        """
        item = self._mem.get(item_id)
        if item is None:
            return f"Not found: {item_id}"

        if properties is not None:
            props = dict(properties)
        else:
            props = {}
            if content is not None:
                props["content"] = content
            if metadata is not None:
                existing_meta = {}
                if hasattr(item, "metadata") and isinstance(item.metadata, dict):
                    existing_meta = item.metadata
                elif isinstance(item, dict):
                    existing_meta = item.get("metadata") or {}
                props["metadata"] = {**existing_meta, **metadata}

        if not props:
            return "At least one of 'content', 'metadata', or 'properties' must be provided"

        self._mem.update_properties(item_id, props, write_mode=write_mode)
        return f"Updated: {item_id}"

    def delete(self, item_id: str, **kwargs: Any) -> bool:
        """Delete a memory by ID."""
        return self._mem.delete(item_id)

    # -- Search --

    def search(self, query: str, top_k: int = 5, **kwargs: Any) -> list[MemoryResult]:
        """Semantic search."""
        from smartmemory_app.storage import search

        results = normalize_items(search(query, top_k, **kwargs))
        # SELF-IMPROVE-6: track shown IDs for local-mode feedback
        self._last_search_session_id = f"local:{id(results)}:{top_k}"
        self._last_shown_ids = [
            r.get("item_id", "") for r in results if r.get("item_id")
        ]
        return results

    def search_by_metadata(
        self, metadata_key: str, metadata_value: str, top_k: int = 10, **kwargs: Any
    ) -> list[MemoryResult]:
        """Search via the public metadata facade; predicates run before LIMIT."""
        if not metadata_key:
            raise ValueError("metadata_key is required.")
        hits = self._mem.search_by_metadata(
            {metadata_key: metadata_value},
            top_k=top_k,
            **{k: kwargs[k] for k in ("since", "until") if kwargs.get(k) is not None},
        )
        return normalize_items(hits)

    def blame_code(self, **kwargs: Any) -> dict[str, Any]:
        """Code-provenance blame passthrough (CORE-CODE-PROVENANCE-1 Phase 2b).

        Delegates to the in-process lite SmartMemory, which reads the same local
        store the capture hook wrote to. Returns the forge-shaped BlameResult dict;
        raises ValueError on a git error (the tool maps it to a graceful string).
        """
        return self._mem.blame_code(**kwargs)

    def recall_pack(self, **kwargs: Any) -> dict[str, Any]:
        """Budgeted context assembly passthrough (CORE-RECALL-BUDGET-1).

        Delegates to the in-process SmartMemory facade, which owns the section gathering
        and the packing policy. Returns the RecallPack dict unchanged.
        """
        return self._mem.recall_pack(**kwargs)

    def policy_bundle(
        self,
        workflow: str | None = None,
        domain: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Compile the in-process workspace's policy bundle unchanged."""
        return self._mem.compile_policy_bundle(workflow=workflow, domain=domain)

    def peer_chat(self, **kwargs: Any) -> dict[str, Any]:
        """Zero-schema peer synthesis passthrough (CORE-ZERO-SCHEMA-1 Phase 1).

        Delegates to the in-process lite SmartMemory, which owns the gather + single
        synthesis call. Local-only — no hosted REST route exists yet.
        """
        from smartmemory.peer.synthesis import peer_chat

        return peer_chat(self._mem, **kwargs)

    def read_transcript_centered(self, **kwargs: Any) -> dict[str, Any]:
        """Centered transcript-reader passthrough (CORE-CODE-PROVENANCE-1 Phase 2c).

        The read half of the blame->read chain. Delegates to the in-process lite
        SmartMemory, which reads the raw transcript JSONL on the local filesystem.
        Local-only — the hosted REST surface is parked.
        """
        return self._mem.read_transcript_centered(**kwargs)

    # -- Ingest & Recall --

    def ingest(self, content: str, memory_type: str = "episodic", **kwargs: Any) -> str:
        """Full pipeline ingestion."""
        from smartmemory_app.storage import ingest

        # storage.ingest() uses 'properties' not 'metadata'
        if "metadata" in kwargs:
            kwargs["properties"] = kwargs.pop("metadata")
        # DIST-LITE-QUIET-1: attribute the write (the /remember skill + MCP memory_ingest
        # surface) so it lands as tier-2 user content, not origin='unknown'. A caller-
        # supplied origin is preserved.
        from smartmemory import resolve_origin

        context = dict(kwargs.pop("context", None) or {})
        kwargs["origin"] = resolve_origin(
            context, origin=kwargs.get("origin"), default="mcp:memory_ingest"
        )
        context.pop("origin", None)
        if context:
            kwargs["properties"] = {**(kwargs.get("properties") or {}), **context}
        return ingest(content, memory_type, **kwargs)

    def recall(self, cwd: str | None = None, top_k: int = 10, **kwargs: Any) -> str:
        """Context-aware recall."""
        from smartmemory_app.storage import recall

        return recall(cwd, top_k)

    def ingest_document(
        self,
        source: str,
        *,
        source_type: str = "auto",
        chunk_size: int = 2000,
        chunk_strategy: str = "paragraph",
        reference: bool = False,
    ) -> dict[str, Any]:
        """Ingest a URL or local file through the core document facade."""
        return self._mem.ingest_document(
            source,
            source_type=source_type,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            reference=reference,
        )

    def ingest_structured(
        self,
        data: dict,
        schema: str | None = None,
        schema_name: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Structured data ingestion."""
        name = schema or schema_name
        return self._mem.ingest_structured(data, schema=name)

    def ingest_conversation_sync(
        self,
        turns: list,
        session_boundaries: list | None = None,
        conversation_id: str | None = None,
        session_dates: list | None = None,
        turns_per_chunk: int = 15,
        max_chunk_chars: int = 12000,
        max_concurrent: int = 4,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Conversation bulk ingestion via local SmartMemory (RLM-1g)."""
        from dataclasses import asdict

        response = self._mem.ingest_conversation_sync(
            turns,
            session_boundaries=session_boundaries,
            conversation_id=conversation_id,
            session_dates=session_dates,
            turns_per_chunk=turns_per_chunk,
            max_chunk_chars=max_chunk_chars,
            max_concurrent=max_concurrent,
        )
        return (
            asdict(response) if hasattr(response, "__dataclass_fields__") else response
        )

    def read_around(
        self,
        item_id: str,
        char_budget: int = 20000,
        before_ratio: float = 0.3,
        after_ratio: float = 0.7,
        cursor: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Auto-centered conversation read via local SmartMemory (CORE-RECALL-CENTERED-1 P2).

        Returns a char-budgeted asymmetric window of the conversation chunks
        surrounding ``item_id``. Hard-truncates (raise_on_overflow=False) rather
        than raising, so the MCP tool always returns a window.
        """
        return self._mem.read_around(
            item_id,
            char_budget=char_budget,
            before_ratio=before_ratio,
            after_ratio=after_ratio,
            cursor=cursor,
            raise_on_overflow=False,
        )

    # -- Listing & Stats --

    def list_memories(
        self, limit: int = 100, offset: int = 0, **kwargs: Any
    ) -> list[MemoryResult]:
        """List memories with pagination, optionally filtered by metadata.

        `smartmemory.SmartMemory` has no `list_memories` — this delegation
        raised AttributeError on every local-mode call (verified 2026-08-02).
        There is no core listing API to wire to: `get_all_items_debug()` returns
        `{total_items, items_by_type, sample_items}`, i.e. a stats summary with
        SAMPLE items, not a complete page. Listing is therefore served from the
        same scan `search_by_metadata` uses, and paginated locally.
        """
        mkey, mval = kwargs.get("metadata_key"), kwargs.get("metadata_value")
        if (mkey is None) != (mval is None):
            missing = "metadata_value" if mkey is not None else "metadata_key"
            raise ValueError(
                f"metadata_key and metadata_value must be supplied together; {missing} is missing."
            )

        window = max((limit + offset) * 10, 200)
        hits = self._mem.search("*", top_k=window)
        items = [normalize_item(raw) for raw in hits or []]
        if mkey is not None:
            items = [
                i
                for i in items
                if _metadata_matches(i.get("metadata") or {}, mkey, mval)
            ]
        if len(hits or []) >= window:
            log.warning(
                "list_memories scanned the local cap (%d items); results beyond that window "
                "are not represented in this page or its offset.",
                window,
            )
        return items[offset : offset + limit]

    def clear_user_memories(self, confirm: bool = False, **kwargs: Any) -> str:
        """Clear all user memories.

        Core exposes `clear()`, not `clear_user_memories` — the old delegation
        raised AttributeError, so this tool never cleared anything in local mode
        (verified 2026-08-02 against the core facade).
        """
        if not confirm:
            return "Pass confirm=True to clear all memories."
        self._mem.clear()
        return "All memories cleared."

    def get_all_items_debug(self, **kwargs: Any) -> dict:
        """Get debug stats."""
        return self._mem.get_all_items_debug()

    def stats(self, **kwargs: Any) -> dict:
        """Memory statistics."""
        return self.get_all_items_debug(**kwargs)

    # -- Evolution --

    def run_evolution_cycle(self, **kwargs: Any) -> dict:
        """Trigger evolution cycle."""
        return self._mem.run_evolution_cycle(**kwargs)

    # CORE-MEMORY-DYNAMICS-1 M1b: commit_working_to_* removed — core façades
    # are gone, ConsolidationRouter routes at ingest. Use add()/ingest() with
    # memory_type="pending" instead.

    def run_evolver(self, evolver_name: str, **kwargs: Any) -> dict:
        """Run a specific evolver by name."""
        return self._mem.run_evolver(evolver_name, **kwargs)

    def run_clustering(self, **kwargs: Any) -> dict:
        """Run clustering."""
        return self._mem.run_clustering(**kwargs)

    # -- Insight --

    def reflect(self, **kwargs: Any) -> str:
        """Reflective analysis."""
        return self._mem.reflect(**kwargs)

    def summary(self, **kwargs: Any) -> dict:
        """Memory summary."""
        return self._mem.summary(**kwargs)

    def orphaned_notes(self, **kwargs: Any) -> list[MemoryResult]:
        """Find orphaned notes."""
        return normalize_items(self._mem.orphaned_notes(**kwargs))

    def find_old_notes(self, days: int = 90, **kwargs: Any) -> list[MemoryResult]:
        """Find notes older than the given number of days."""
        return normalize_items(self._mem.find_old_notes(days, **kwargs))

    def personalize(
        self,
        user_id: str = "mcp-user",
        traits: dict | None = None,
        preferences: dict | None = None,
        **kwargs: Any,
    ) -> str:
        """Personalize memory system."""
        return self._mem.personalize(
            user_id=user_id,
            traits=traits or {},
            preferences=preferences or {},
            **kwargs,
        )

    def update_from_feedback(
        self, feedback: dict | None = None, memory_type: str = "semantic", **kwargs: Any
    ) -> str:
        """Update from user feedback."""
        return self._mem.update_from_feedback(
            feedback=feedback or {}, memory_type=memory_type, **kwargs
        )

    def ground(self, item_id: str, **kwargs: Any) -> dict:
        """Ground a memory item."""
        return self._mem.ground(item_id=item_id, **kwargs)

    # -- Graph --

    def link(
        self,
        source_id: str,
        target_id: str,
        link_type: str = "RELATES_TO",
        **kwargs: Any,
    ) -> str:
        """Link two memories."""
        return self._mem.link(source_id, target_id, link_type=link_type)

    def add_edge(
        self, source_id: str, target_id: str, relation_type: str, **kwargs: Any
    ) -> str:
        """Add a graph edge."""
        return self._mem.add_edge(
            source_id, target_id, relation_type=relation_type, **kwargs
        )

    def get_links(self, item_id: str, **kwargs: Any) -> list[MemoryResult]:
        """Get links for an item."""
        return normalize_items(self._mem.get_links(item_id))

    def get_neighbors(self, item_id: str, **kwargs: Any) -> dict:
        """Get graph neighbors."""
        return self._mem.get_neighbors(item_id)

    def find_shortest_path(self, start_id: str, end_id: str, **kwargs: Any) -> list:
        """Find shortest path between two items."""
        return self._mem.find_shortest_path(start_id, end_id, **kwargs)

    # -- Auth (no-ops for local) --

    def login(self, api_key: str, **kwargs: Any) -> str:
        """No-op in local mode."""
        return "Local mode — no authentication required."

    def whoami(self, **kwargs: Any) -> str:
        """Local mode session info."""
        return "Local mode — single user."

    def switch_team(self, team_id: str, **kwargs: Any) -> str:
        """No-op in local mode."""
        return "Local mode — teams not applicable."

    # -- Retrieval feedback (SELF-IMPROVE-6) --

    def submit_feedback(
        self, search_session_id: str, result_used: list[str], **kwargs: Any
    ) -> dict:
        """Emit result-selection feedback via core retrieval_tracking (local mode).

        Uses the shown_ids captured from the most recent search() call to provide
        accurate selection rate data (not just result_used == result_shown).
        """
        try:
            from smartmemory.observability.retrieval_tracking import (
                emit_result_feedback,
            )

            # Use shown_ids from the search that produced these results
            shown_ids = getattr(self, "_last_shown_ids", None) or []
            if not shown_ids:
                log.warning(
                    "submit_feedback: no prior search() call — shown_ids unknown, skipping"
                )
                return {"status": "skipped", "reason": "no prior search context"}

            emit_result_feedback(
                query_hash="",
                result_used=result_used,
                result_shown=shown_ids,
                workspace_id="default",
                search_session_id=search_session_id,
            )
            return {
                "status": "ok",
                "search_session_id": search_session_id,
                "result_used_count": len(result_used),
            }
        except Exception as exc:
            return {"error": str(exc)}

    # -- Decision lifecycle (MCP-REMOTE-DECISIONS-1) --
    #
    # The managed-type Managers/Queries take the REAL SmartMemory (``self._mem``),
    # never this wrapper: ``DecisionManager`` calls ``sm.add(MemoryItem)`` and
    # ``sm._graph``, and passing the wrapper silently re-wrapped the MemoryItem as
    # ``content`` — a successful-looking write that discarded decision_type,
    # confidence, rationale and the deterministic id (2026-06-02 bug hunt).
    # Living here rather than in the tool is what lets the tool stay
    # transport-agnostic; the ``_mem`` requirement is unchanged.

    def _decision_manager(self):
        from smartmemory.decisions.manager import DecisionManager

        return DecisionManager(self._mem)

    def _decision_queries(self):
        from smartmemory.decisions.queries import DecisionQueries

        return DecisionQueries(self._mem)

    def _residuation(self):
        from smartmemory.reasoning.residuation import ResiduationManager

        return ResiduationManager(self._mem)

    def decision_create(self, content: str, **kwargs: Any) -> dict[str, Any]:
        """DecisionManager.create, serialized to the wire shape."""
        decision = self._decision_manager().create(
            content=content,
            decision_type=kwargs.get("decision_type", "inference"),
            confidence=kwargs.get("confidence", 0.8),
            source_trace_id=kwargs.get("source_trace_id"),
            evidence_ids=kwargs.get("evidence_ids") or [],
            domain=kwargs.get("domain"),
            tags=kwargs.get("tags") or [],
            rejected_alternatives=kwargs.get("rejected_alternatives") or [],
            rationale=kwargs.get("rationale"),
            constraints=kwargs.get("constraints") or [],
        )
        return _decision_dict(decision)

    def decision_get(self, decision_id: str) -> dict[str, Any] | None:
        decision = self._decision_manager().get_decision(decision_id)
        return _decision_dict(decision) if decision else None

    def decision_list(
        self,
        domain: str | None = None,
        decision_type: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        decisions = self._decision_queries().get_active_decisions(
            domain=domain,
            decision_type=decision_type,
            min_confidence=min_confidence,
            limit=limit,
        )
        return [_decision_dict(d) for d in decisions]

    def decision_search(self, topic: str, limit: int = 20) -> list[dict[str, Any]]:
        decisions = self._decision_queries().get_decisions_about(
            topic=topic, limit=limit
        )
        return [_decision_dict(d) for d in decisions]

    def decision_supersede(
        self,
        decision_id: str,
        new_content: str,
        reason: str,
        new_decision_type: str = "inference",
        new_confidence: float = 0.8,
        rejected_alternatives: list[str] | None = None,
        rationale: str | None = None,
        constraints: list[str] | None = None,
    ) -> dict[str, Any] | None:
        from smartmemory.models.decision import Decision

        new_decision = Decision(
            content=new_content,
            decision_type=new_decision_type,
            confidence=new_confidence,
        )
        try:
            result = self._decision_manager().supersede(
                decision_id,
                new_decision,
                reason=reason,
                rejected_alternatives=rejected_alternatives,
                rationale=rationale,
                constraints=constraints,
            )
        except ValueError:
            # Core raises ValueError for "no such decision"; the service answers
            # 404 for the same case. Both arms report absence as None.
            return None
        return {
            "old_decision_id": decision_id,
            "new_decision_id": result.decision_id,
            "status": "superseded",
        }

    def decision_retract(self, decision_id: str, reason: str) -> dict[str, Any] | None:
        try:
            self._decision_manager().retract(decision_id, reason=reason)
        except ValueError:
            return None
        return {"decision_id": decision_id, "status": "retracted"}

    def decision_reinforce(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        try:
            decision = self._decision_manager().reinforce(decision_id, evidence_id)
        except ValueError:
            return None
        return {
            "decision_id": decision_id,
            "confidence": decision.confidence,
            "reinforcement_count": decision.reinforcement_count,
        }

    def decision_contradict(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        try:
            decision = self._decision_manager().contradict(decision_id, evidence_id)
        except ValueError:
            return None
        return {
            "decision_id": decision_id,
            "confidence": decision.confidence,
            "contradiction_count": decision.contradiction_count,
        }

    def decision_provenance(self, decision_id: str) -> dict[str, Any] | None:
        provenance = self._decision_queries().get_decision_provenance(decision_id)
        if not provenance or provenance.get("decision") is None:
            return None
        return {
            "decision": _decision_dict(provenance["decision"]),
            "reasoning_trace": provenance.get("reasoning_trace"),
            "evidence": provenance.get("evidence") or [],
            "superseded": [
                _decision_dict(d) for d in (provenance.get("superseded") or [])
            ],
            "superseded_by": [
                _decision_dict(d) for d in (provenance.get("superseded_by") or [])
            ],
        }

    def decision_find_conflicts(self, decision_id: str) -> dict[str, Any] | None:
        manager = self._decision_manager()
        decision = manager.get_decision(decision_id)
        if not decision:
            return None
        conflicts = manager.find_conflicts(decision)
        return {
            "decision_id": decision_id,
            "conflicts": [_decision_dict(c) for c in conflicts],
            "count": len(conflicts),
        }

    def decision_create_pending(
        self,
        content: str,
        requirements: list[dict[str, Any]],
        domain: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        decision = self._residuation().create_pending(
            content=content,
            requirements=requirements,
            domain=domain,
            tags=tags or [],
        )
        return _decision_dict(decision)

    def decision_resolve_requirement(
        self, decision_id: str, requirement_id: str, memory_id: str
    ) -> bool:
        return bool(
            self._residuation().resolve_requirement(
                decision_id, requirement_id, memory_id
            )
        )

    def decision_try_activate(self, decision_id: str) -> bool:
        return bool(self._residuation().try_activate(decision_id))
