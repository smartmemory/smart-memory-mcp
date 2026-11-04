"""MemoryBackend protocol — structural interface for local and remote backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import MemoryResult


# MCP-LOCAL-BROKEN-TOOLS-1: tools that register but only error in local mode.
# tool name -> the bug that breaks it. Empty since SM-LOCAL-TOOLS-1 fixed all fourteen; the
# mechanism stays for the next one. Each tool is its own capability (named after
# the tool, not a backend method): LocalBackend declares them unsupported, remote and
# hosted backends keep them. Delete a row here when its bug is fixed; the tool comes
# back in local mode with no other change. Tool names in capabilities.TOOL_CAPABILITIES.
LOCAL_BROKEN_TOOLS: dict[str, str] = {}


class EvidenceRefused(ValueError):
    """SM-EVIDENCE-TYPE-1: the store refused an evidence write or link, with the cause in the message.

    Local mode re-raises core's ValueError as this; remote mode raises it for the route's 404/409/422. A path-guard
    refusal (``path_guard.path_id``) stays a plain ValueError, so the tools render only store refusals as text.
    """


class BackendCapabilities:
    """Central capability check, including explicitly unsupported operations."""

    unsupported_capabilities: frozenset[str] = frozenset()

    @classmethod
    def supports(cls, method: str) -> bool:
        # Tool-level capabilities (LOCAL_BROKEN_TOOLS) have no backend method.
        return method not in cls.unsupported_capabilities and (
            method in LOCAL_BROKEN_TOOLS or callable(getattr(cls, method, None))
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

    def ingest_code(
        self,
        directory: str,
        repo: str,
        exclude_dirs: list[str] | None = None,
        languages: list[str] | None = None,
    ) -> Any:
        """Index a client checkout and return the core IndexResult summary fields."""
        ...

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

    # --- Receipt-reading saves (U4.42-FU-TOOLS): the save tools' one seam ---------
    # Each returns a ``SaveReturn``: the legacy value on the legacy arm, the receipt plus its summary on the receipt arm.

    def add_with_receipt(
        self, content: str, memory_type: str = "semantic", **kwargs: Any
    ) -> Any:
        """``add`` that keeps the save receipt."""
        ...

    def ingest_with_receipt(
        self, content: str, memory_type: str = "semantic", **kwargs: Any
    ) -> Any:
        """``ingest`` that keeps the save receipt."""
        ...

    def ingest_structured_with_receipt(
        self, data: dict[str, Any], schema: str | None = None, **kwargs: Any
    ) -> Any:
        """``ingest_structured`` that keeps the save receipt."""
        ...

    def ingest_document_with_receipt(self, source: str, **kwargs: Any) -> Any:
        """``ingest_document`` that keeps the save receipt."""
        ...

    def ingest_conversation_with_receipt(self, turns: list, **kwargs: Any) -> Any:
        """``ingest_conversation_sync`` that keeps the save receipt."""
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

    def decision_get(
        self, decision_id: str, as_of: str | None = None
    ) -> dict[str, Any] | None:
        """Full `Decision.to_dict()`, or None when the decision does not exist (at `as_of` when set)."""
        ...

    def decision_list(
        self,
        domain: str | None = None,
        decision_type: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Active decisions, filtered (active at `as_of` when set, SM-ASOF-DECISIONS-1)."""
        ...

    def decision_search(
        self, topic: str, limit: int = 20, as_of: str | None = None
    ) -> list[dict[str, Any]]:
        """Active decisions related to a topic (active at `as_of` when set)."""
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

    def decision_link_evidence(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any]:
        """SM-EVIDENCE-TYPE-1: link a stored evidence node (node id or ``ev:`` ref) to a live decision.

        {decision_id, evidence_id (the node id), evidence_ids, confidence}; confidence is unchanged. Raises
        EvidenceRefused naming the refusal: decision or evidence not found, a node of another type, an ended decision.
        """
        ...

    def decision_provenance(
        self, decision_id: str, as_of: str | None = None
    ) -> dict[str, Any] | None:
        """{decision, reasoning_trace, evidence, superseded, superseded_by}, or None when absent (at `as_of`)."""
        ...

    def evidence_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """SM-EVIDENCE-TYPE-1: store one Compose ``ev:`` record as an `evidence` node (idempotent on its id).

        The contract's evidence_response: {evidence_id, evidence_ref, kind, subject, checker, observed_at,
        checked_at, checks, payload, record}. Raises EvidenceRefused for a malformed record or a different body
        under a stored id.
        """
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

    # --- Patterns, plans, anchors, failure journal (SM-LOCAL-TOOLS-1) ------------
    # Plain dicts/lists/strings, as the decision block. Local runs the core managers on
    # the real store; remote raises NotImplementedError (no client for the routes yet).

    def pattern_query(
        self,
        scope: str | None = None,
        severity: str | None = None,
        free_text: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def pattern_get(self, pattern_id: str) -> dict[str, Any] | None: ...

    def pattern_list(self, limit: int = 200) -> list[dict[str, Any]]: ...

    def plan_create(
        self,
        title: str,
        tasks: list[dict[str, Any]] | None = None,
        context: str = "",
        created_by: str = "",
    ) -> dict[str, Any]: ...

    def plan_get(self, plan_id: str) -> dict[str, Any] | None: ...

    def plan_active(self) -> list[dict[str, Any]]: ...

    def plan_update_task(self, plan_id: str, task_id: str, status: str) -> None: ...

    def anchor_set(self, content: str, anchor_type: str, session_id: str) -> str: ...

    def anchor_list(
        self, session_id: str = "", anchor_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def anchor_clear(
        self, session_id: str = "", anchor_type: str | None = None
    ) -> int: ...

    def anchor_graduate(self, session_id: str = "") -> list[str]: ...

    def anchor_check_drift(
        self, session_id: str, recent_outputs: list[str]
    ) -> list[dict[str, Any]]: ...

    def log_failure(
        self, error_type: str, content: str, context: str, **kwargs: Any
    ) -> str: ...

    def check_failure(
        self, error_type: str, context: str, top_k: int = 3
    ) -> list[dict[str, Any]]: ...
