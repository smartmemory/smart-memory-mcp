"""Reasoning MCP tools — challenge assertions, resolve conflicts, query routing, proof trees."""

import logging

from mcp.types import ToolAnnotations

from .common import get_backend, graceful

logger = logging.getLogger(__name__)

_GRAPH_UNAVAILABLE_MSG = (
    "Requires graph backend (FalkorDB). "
    "Available in local mode with full infrastructure."
)


def register(mcp):
    """Register reasoning tools with the MCP server."""

    @mcp.tool()
    @graceful
    def reasoning_challenge(
        assertion: str,
        memory_type: str = "semantic",
        use_llm: bool = True,
    ) -> str:
        """Challenge an assertion against existing knowledge to detect contradictions."""
        from smartmemory.reasoning.challenger import AssertionChallenger

        backend = get_backend()
        # Challenger expects MemoryItem objects — requires local SmartMemory instance
        sm = getattr(backend, "_mem", None)
        if sm is None:
            return "reasoning_challenge requires local backend (smartmemory package)"
        challenger = AssertionChallenger(
            sm,
            use_llm=use_llm,
            use_graph=True,
            use_embedding=True,
            use_heuristic=True,
        )

        result = challenger.challenge(assertion, memory_type=memory_type)

        parts = [
            f"Challenge result for: {assertion[:80]}...",
            f"Has conflicts: {result.has_conflicts}",
            f"Overall confidence: {result.overall_confidence:.2f}",
            f"Related facts: {len(result.related_facts)}",
        ]

        if result.conflicts:
            parts.append(f"\nConflicts ({len(result.conflicts)}):")
            for c in result.conflicts:
                parts.append(
                    f"  - [{c.existing_item.item_id}] {c.conflict_type.value} "
                    f"(conf={c.confidence:.2f}): {c.explanation[:100]}"
                )

        return "\n".join(parts)

    @mcp.tool()
    @graceful
    def reasoning_resolve_conflict(
        existing_item_id: str,
        new_fact: str,
        use_wikipedia: bool = True,
        use_llm: bool = True,
    ) -> str:
        """Resolve a conflict between assertions via Wikipedia, LLM, grounding, or recency."""
        from smartmemory.reasoning.challenger import (
            AssertionChallenger,
            Conflict,
            ConflictType,
            ResolutionStrategy,
        )

        backend = get_backend()
        # Challenger expects MemoryItem objects — requires local SmartMemory instance
        sm = getattr(backend, "_mem", None)
        if sm is None:
            return "reasoning_resolve_conflict requires local backend (smartmemory package)"
        existing_item = sm.get(existing_item_id)
        if not existing_item:
            return f"Memory item not found: {existing_item_id}"

        conflict = Conflict(
            existing_item=existing_item,
            existing_fact=existing_item.content,
            new_fact=new_fact,
            conflict_type=ConflictType.DIRECT_CONTRADICTION,
            confidence=0.8,
            explanation="Conflict submitted for resolution",
            suggested_resolution=ResolutionStrategy.DEFER,
        )

        challenger = AssertionChallenger(sm, use_llm=use_llm)
        result = challenger.auto_resolve(
            conflict, use_wikipedia=use_wikipedia, use_llm=use_llm
        )

        parts = [
            f"Auto-resolved: {result.get('auto_resolved', False)}",
            f"Method: {result.get('method', 'N/A')}",
            f"Confidence: {result.get('confidence', 0.0):.2f}",
        ]
        if result.get("evidence"):
            parts.append(f"Evidence: {result['evidence'][:200]}")
        if result.get("actions_taken"):
            parts.append(f"Actions: {', '.join(result['actions_taken'])}")

        return "\n".join(parts)

    @mcp.tool()
    @graceful
    def reasoning_query(query: str, top_k: int = 10) -> str:
        """Route a query to the cheapest effective retrieval method (symbolic, semantic, or hybrid)."""
        from smartmemory.reasoning.query_router import QueryRouter

        backend = get_backend()
        router_instance = QueryRouter(backend)
        result = router_instance.route(query, top_k=top_k)

        parts = [
            f"Query type: {result['query_type']}",
            f"Results: {result['result_count']}",
        ]

        for r in result.get("results", [])[:5]:
            if r.get("content"):
                preview = (
                    r["content"][:150] + "..."
                    if len(r["content"]) > 150
                    else r["content"]
                )
                parts.append(f"  - [{r['item_id']}] {preview}")
            else:
                parts.append(f"  - {str(r)[:150]}")

        return "\n".join(parts)

    @mcp.tool()
    @graceful
    def reasoning_proof_tree(decision_id: str, max_depth: int = 5) -> str:
        """Build an auditable proof tree for a decision, tracing evidence back to sources."""
        from smartmemory.reasoning.proof_tree import ProofTreeBuilder

        backend = get_backend()
        # Backends expose the SmartMemory instance as `_mem` (see local.py); the
        # graph hangs off that, not off the backend itself.
        sm = getattr(backend, "_mem", None)
        graph = getattr(sm, "graph", None) if sm is not None else None

        if not graph:
            return _GRAPH_UNAVAILABLE_MSG

        builder = ProofTreeBuilder(graph)
        tree = builder.build_proof(decision_id, max_depth=max_depth)

        if tree is None:
            return f"Decision not found: {decision_id}"

        return tree.render_text()

    @mcp.tool()
    @graceful
    def reasoning_fuzzy_confidence(decision_id: str) -> str:
        """Get multi-dimensional confidence score for a decision (evidence, recency, consensus, directness)."""
        from smartmemory.decisions.manager import DecisionManager
        from smartmemory.reasoning.fuzzy_confidence import FuzzyConfidenceCalculator

        backend = get_backend()
        # DecisionManager takes the SmartMemory instance (cf. smart_memory.py's
        # `DecisionManager(self)`), not the MCP backend wrapper.
        sm = getattr(backend, "_mem", None)
        if sm is None:
            return _GRAPH_UNAVAILABLE_MSG

        dm = DecisionManager(sm)
        decision = dm.get_decision(decision_id)
        if not decision:
            return f"Decision not found: {decision_id}"

        graph = getattr(sm, "graph", None)
        if not graph:
            return _GRAPH_UNAVAILABLE_MSG

        calc = FuzzyConfidenceCalculator(graph)
        score = calc.calculate(decision)

        return f"Fuzzy confidence for {decision_id}:\n{score.to_dict()}"

    @mcp.tool()
    @graceful
    def reasoning_extract_trace(
        content: str,
        min_steps: int = 2,
        use_llm_detection: bool = True,
    ) -> str:
        """Extract reasoning traces from content — detects chain-of-thought patterns."""
        from smartmemory.plugins.extractors.reasoning import (
            ReasoningExtractor,
            ReasoningExtractorConfig,
        )

        config = ReasoningExtractorConfig(
            min_steps=min_steps,
            min_quality_score=0.4,
            use_llm_detection=use_llm_detection,
        )
        extractor = ReasoningExtractor(config=config)
        result = extractor.extract(content)
        trace = result.get("reasoning_trace")

        if not trace:
            return "No reasoning trace detected in content."

        parts = [
            f"Reasoning trace: {trace.trace_id}",
            f"Steps: {len(trace.steps)}",
            f"Has explicit markup: {trace.has_explicit_markup}",
        ]
        if trace.evaluation:
            parts.append(f"Quality score: {trace.evaluation.quality_score:.2f}")

        for i, step in enumerate(trace.steps, 1):
            parts.append(f"  {i}. [{step.type}] {step.content[:100]}")

        return "\n".join(parts)

    @mcp.tool(
        title="Query reasoning traces",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def reasoning_query_traces(query: str, limit: int = 10) -> str:
        """Query stored reasoning traces — ask 'why' questions about past decisions."""
        backend = get_backend()
        results = backend.search(query, memory_type="reasoning", top_k=limit)

        if not results:
            return f"No reasoning traces found for: {query}"

        output = [f"Found {len(results)} reasoning traces:\n"]
        for item in results:
            meta = item["metadata"]
            steps = meta.get("steps", [])
            item_id = item["item_id"]
            content = item["content"]
            output.append(f"- [{item_id}] ({len(steps)} steps): {content[:120]}")

        return "\n".join(output)
