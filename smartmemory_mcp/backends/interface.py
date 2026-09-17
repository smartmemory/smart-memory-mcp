"""MemoryBackend protocol — structural interface for local and remote backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import MemoryResult


class BackendCapabilities:
    """Central capability check, including explicitly unsupported operations."""

    unsupported_capabilities: frozenset[str] = frozenset()

    @classmethod
    def supports(cls, method: str) -> bool:
        return method not in cls.unsupported_capabilities and callable(
            getattr(cls, method, None)
        )


@runtime_checkable
class MemoryBackend(Protocol):
    """Duck-typed interface that all backend implementations must satisfy.

    Tools call these methods without knowing whether the backend is local (in-process
    SmartMemory) or remote (REST API via httpx). Methods use **kwargs liberally so
    backends can accept extra parameters without protocol changes.
    """

    def supports(self, method: str) -> bool:
        """Whether this backend implements an operation."""
        ...

    # --- Core CRUD ---------------------------------------------------------------

    def add(
        self, content: str, memory_type: str = "semantic", **kwargs: Any
    ) -> dict[str, Any]:
        """Store a memory item."""
        ...

    def get(self, item_id: str, **kwargs: Any) -> MemoryResult | None:
        """Get a single memory by ID."""
        ...

    def update(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Update a memory item."""
        ...

    def delete(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Delete a memory item."""
        ...

    # --- Search & Recall ---------------------------------------------------------

    def search(self, query: str, top_k: int = 5, **kwargs: Any) -> list[MemoryResult]:
        """Semantic similarity search."""
        ...

    def search_by_metadata(
        self, metadata_key: str, metadata_value: str, top_k: int = 10, **kwargs: Any
    ) -> list[MemoryResult]:
        """Search by metadata key-value match."""
        ...

    def explain(self, memory_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """Full provenance answer for one memory (PLAT-AUDITABLE-MEMORY-1 explain contract)."""
        ...

    def recall(self, cwd: str | None = None, top_k: int = 10, **kwargs: Any) -> str:
        """Recall recent and relevant memories, formatted as markdown."""
        ...

    def policy_bundle(
        self,
        workflow: str | None = None,
        domain: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Compile the active workspace's policy bundle for an enforcement runner."""
        ...

    # --- Pipeline ----------------------------------------------------------------

    def ingest(
        self, content: str, memory_type: str = "semantic", **kwargs: Any
    ) -> dict[str, Any] | str:
        """Ingest content through the full extraction pipeline."""
        ...

    def ingest_structured(
        self, data: dict[str, Any], schema: str | None = None, **kwargs: Any
    ) -> str:
        """Ingest structured data with an optional schema."""
        ...

    def ingest_document(
        self,
        source: str,
        *,
        source_type: str = "auto",
        chunk_size: int = 2000,
        chunk_strategy: str = "paragraph",
        reference: bool = False,
    ) -> dict[str, Any]:
        """Return document_id, chunk_ids, and a status:

        ingested: a new document and its chunks were created.
        existing: the document was already fully present.
        resumed: a prior incomplete ingest created its missing chunks.
        """
        ...

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
        """Ingest a conversation as session chunks (RLM-1g)."""
        ...

    # --- Collection operations ---------------------------------------------------

    def list_memories(self, **kwargs: Any) -> dict[str, Any] | list[MemoryResult]:
        """List memory items, preserving pagination metadata when available."""
        ...

    def clear_user_memories(self, **kwargs: Any) -> dict[str, Any]:
        """Delete all memories for the current user."""
        ...

    def get_all_items_debug(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all items including internal nodes (debug only)."""
        ...

    def stats(self, **kwargs: Any) -> dict[str, Any]:
        """Return memory statistics (total, by type, health score)."""
        ...

    # --- Evolution & clustering --------------------------------------------------

    def run_evolution_cycle(self, **kwargs: Any) -> dict[str, Any]:
        """Run a full evolution cycle across all evolvers."""
        ...

    # CORE-MEMORY-DYNAMICS-1 M1b: commit_working_to_episodic / commit_working_to_procedural
    # removed from the protocol. The core façades are gone — the ConsolidationRouter
    # now routes pending items at ingest. Backends that previously implemented these
    # methods should drop them; callers should use add()/ingest() with
    # memory_type="pending".

    def run_evolver(self, evolver_name: str, **kwargs: Any) -> dict[str, Any]:
        """Run a specific evolver by name."""
        ...

    def run_clustering(self, **kwargs: Any) -> dict[str, Any]:
        """Run clustering analysis on stored memories."""
        ...

    # --- Insight & reflection ----------------------------------------------------

    def reflect(self, **kwargs: Any) -> dict[str, Any]:
        """Generate reflections from stored memories."""
        ...

    def summary(self, **kwargs: Any) -> dict[str, Any]:
        """Generate a summary of stored memories."""
        ...

    def orphaned_notes(self, **kwargs: Any) -> list[MemoryResult]:
        """Find notes without links to other memories."""
        ...

    def find_old_notes(self, **kwargs: Any) -> list[MemoryResult]:
        """Find notes that haven't been accessed recently."""
        ...

    # --- Personalization ---------------------------------------------------------

    def personalize(self, query: str, **kwargs: Any) -> dict[str, Any]:
        """Personalize a response using stored memory context."""
        ...

    def update_from_feedback(
        self, item_id: str, feedback: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Update a memory item based on user feedback."""
        ...

    # --- Grounding & linking -----------------------------------------------------

    def ground(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Ground a memory item with external references."""
        ...

    def link(self, source_id: str, target_id: str, **kwargs: Any) -> dict[str, Any]:
        """Create a link between two memory items."""
        ...

    def add_edge(
        self, source_id: str, target_id: str, relation: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Add a typed edge between two items in the knowledge graph."""
        ...

    def get_links(self, item_id: str, **kwargs: Any) -> list[MemoryResult]:
        """Get all links for a memory item."""
        ...

    def get_neighbors(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Get neighboring nodes in the knowledge graph."""
        ...

    def find_shortest_path(
        self, source_id: str, target_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Find the shortest path between two nodes in the graph."""
        ...

    # --- Retrieval feedback (SELF-IMPROVE-6) -------------------------------------

    def submit_feedback(
        self, search_session_id: str, result_used: list[str], **kwargs: Any
    ) -> dict[str, Any]:
        """Submit result-selection feedback for a completed search session."""
        ...

    # --- Decision lifecycle (MCP-REMOTE-DECISIONS-1) -------------------------------
    #
    # Every method here returns a PLAIN DICT in `Decision.to_dict()` shape (or a
    # list of them, or a bool), never a core `Decision` object. That is what makes
    # the rendered tool output byte-identical between local and remote mode: the
    # tool formats one shape and neither branch on `_mem` nor re-implement
    # rendering. Absence is `None`; every other failure raises.

    def decision_create(self, content: str, **kwargs: Any) -> dict[str, Any]:
        """Create a decision. Returns at least decision_id/decision_type/confidence."""
        ...

    def decision_get(self, decision_id: str) -> dict[str, Any] | None:
        """Full `Decision.to_dict()`, or None when the decision does not exist."""
        ...

    def decision_list(
        self,
        domain: str | None = None,
        decision_type: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Active decisions, filtered."""
        ...

    def decision_search(self, topic: str, limit: int = 20) -> list[dict[str, Any]]:
        """Active decisions related to a topic."""
        ...

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
        """{old_decision_id, new_decision_id, status}, or None when absent."""
        ...

    def decision_retract(self, decision_id: str, reason: str) -> dict[str, Any] | None:
        """{decision_id, status}, or None when the decision does not exist."""
        ...

    def decision_reinforce(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        """{decision_id, confidence, reinforcement_count}, or None when absent."""
        ...

    def decision_contradict(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        """{decision_id, confidence, contradiction_count}, or None when absent."""
        ...

    def decision_provenance(self, decision_id: str) -> dict[str, Any] | None:
        """{decision, reasoning_trace, evidence, superseded, superseded_by}, or None when absent."""
        ...

    def decision_find_conflicts(self, decision_id: str) -> dict[str, Any] | None:
        """{decision_id, conflicts, count}, or None when the decision is absent."""
        ...

    def decision_create_pending(
        self,
        content: str,
        requirements: list[dict[str, Any]],
        domain: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Full `Decision.to_dict()` of the new pending decision."""
        ...

    def decision_resolve_requirement(
        self, decision_id: str, requirement_id: str, memory_id: str
    ) -> bool:
        """True when the requirement was found and marked resolved."""
        ...

    def decision_try_activate(self, decision_id: str) -> bool:
        """True when the pending decision became active."""
        ...
