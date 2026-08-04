"""MemoryBackend protocol — structural interface for local and remote backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import MemoryResult


@runtime_checkable
class MemoryBackend(Protocol):
    """Duck-typed interface that all backend implementations must satisfy.

    Tools call these methods without knowing whether the backend is local (in-process
    SmartMemory) or remote (REST API via httpx). Methods use **kwargs liberally so
    backends can accept extra parameters without protocol changes.
    """

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

    def list_memories(self, **kwargs: Any) -> list[MemoryResult]:
        """List all memory items."""
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
