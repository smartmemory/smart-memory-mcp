"""Decision lifecycle MCP tools — create, query, supersede, retract, reinforce, contradict.

MCP-REMOTE-DECISIONS-1: these tools are transport-agnostic. Every one of them calls
``backend.decision_*``, which both LocalBackend (core managers over ``_mem``) and
RemoteBackend (httpx to ``/memory/decisions/*``) implement, returning the same
``Decision.to_dict()`` dict shape. Rendering therefore happens once, so the text an
agent sees is identical in local and remote mode. Before this, every tool answered
remote callers with a refusal string, which meant an agent on the hosted product —
the primary configuration — could not record a decision at all.
"""

import logging
from typing import Any, Dict, List, Optional

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


# CORE-DECISION-BELIEF-SURFACE-1: summary lines render uncertainty in words, not a
# float. An agent scanning a list should see "this is not settled" without having to
# interpret a Dempster-Shafer interval — and, more importantly, should not read an
# unevidenced decision as an unqualified assertion just because it has a confident
# prior. `ignorance` is evidence-only: 1.0 means nothing has reinforced or
# contradicted it either way, whatever `confidence` says.
_UNCERTAINTY_THRESHOLD = 0.7
_UNCERTAINTY_MARKER = "⟨not enough evidence⟩"


def _field(decision: Any, name: str, default: Any = None) -> Any:
    """Read one field off a decision dict, tolerating a core object.

    Backends hand back `Decision.to_dict()` dicts. The attribute fallback keeps a
    caller-supplied double (or any future backend that forwards a core object)
    from silently rendering blanks.
    """
    if isinstance(decision, dict):
        return decision.get(name, default)
    return getattr(decision, name, default)


def _uncertainty_marker(decision: Any) -> str:
    """Return a words-not-maths uncertainty marker, or an empty string."""
    ignorance = _field(decision, "ignorance")
    if ignorance is None or ignorance <= _UNCERTAINTY_THRESHOLD:
        return ""
    return f" {_UNCERTAINTY_MARKER}"


def _summary_line(decision: Any) -> str:
    """One list/search row. Shared so both tools cannot drift apart."""
    confidence = _field(decision, "confidence") or 0.0
    return (
        f"- [{_field(decision, 'decision_id')}] "
        f"({_field(decision, 'decision_type')}, conf={confidence:.2f}): "
        f"{str(_field(decision, 'content', ''))[:100]}{_uncertainty_marker(decision)}"
    )


def register(mcp):
    """Register decision tools with the MCP server (13 tools)."""

    @mcp.tool()
    @graceful
    def decision_create(
        content: str,
        decision_type: str = "inference",
        confidence: float = 0.8,
        evidence_ids: Optional[List[str]] = None,
        domain: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source_trace_id: Optional[str] = None,
        rejected_alternatives: Optional[List[str]] = None,
        rationale: Optional[str] = None,
        constraints: Optional[List[str]] = None,
    ) -> str:
        """Create a new decision with provenance tracking.

        CORE-EXPERTISE-1 Phase 1: rejected_alternatives, rationale, constraints
        capture the "why" structure that makes decisions usable expertise.
        """
        try:
            backend = get_backend()
            decision = backend.decision_create(
                content=content,
                decision_type=decision_type,
                confidence=confidence,
                source_trace_id=source_trace_id,
                evidence_ids=evidence_ids or [],
                domain=domain,
                tags=tags or [],
                rejected_alternatives=rejected_alternatives or [],
                rationale=rationale,
                constraints=constraints or [],
            )

            lines = [
                f"Decision created: {_field(decision, 'decision_id')}",
                f"Type: {_field(decision, 'decision_type')}",
                f"Confidence: {_field(decision, 'confidence')}",
            ]
            if _field(decision, "rejected_alternatives"):
                lines.append(
                    f"Rejected alternatives: {_field(decision, 'rejected_alternatives')}"
                )
            if _field(decision, "rationale"):
                lines.append(f"Rationale: {_field(decision, 'rationale')}")
            if _field(decision, "constraints"):
                lines.append(f"Constraints: {_field(decision, 'constraints')}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Failed to create decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_get(decision_id: str) -> str:
        """Retrieve a decision by ID."""
        try:
            backend = get_backend()
            decision = backend.decision_get(decision_id)

            if not decision:
                return f"Decision not found: {decision_id}"

            return str(decision)
        except Exception as e:
            logger.error(f"Failed to get decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_list(
        domain: Optional[str] = None,
        decision_type: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> str:
        """List active decisions with optional filtering."""
        try:
            backend = get_backend()
            decisions = backend.decision_list(
                domain=domain,
                decision_type=decision_type,
                min_confidence=min_confidence,
                limit=limit,
            )

            if not decisions:
                return "No active decisions found."

            output = [f"Found {len(decisions)} active decisions:\n"]
            output.extend(_summary_line(d) for d in decisions)
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Failed to list decisions: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_search(topic: str, limit: int = 20) -> str:
        """Search for active decisions related to a topic."""
        try:
            backend = get_backend()
            decisions = backend.decision_search(topic=topic, limit=limit)

            if not decisions:
                return f"No decisions found about: {topic}"

            output = [f"Found {len(decisions)} decisions about '{topic}':\n"]
            output.extend(_summary_line(d) for d in decisions)
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Failed to search decisions: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_supersede(
        decision_id: str,
        new_content: str,
        reason: str,
        new_decision_type: str = "inference",
        new_confidence: float = 0.8,
        rejected_alternatives: Optional[List[str]] = None,
        rationale: Optional[str] = None,
        constraints: Optional[List[str]] = None,
    ) -> str:
        """Replace a decision, preserving its rationale, alternatives and constraints."""
        try:
            backend = get_backend()
            result = backend.decision_supersede(
                decision_id=decision_id,
                new_content=new_content,
                reason=reason,
                new_decision_type=new_decision_type,
                new_confidence=new_confidence,
                rejected_alternatives=rejected_alternatives,
                rationale=rationale,
                constraints=constraints,
            )
            if result is None:
                return f"Decision not found: {decision_id}"

            return (
                f"Decision {decision_id} superseded.\n"
                f"New decision: {result.get('new_decision_id')}"
            )
        except Exception as e:
            logger.error(f"Failed to supersede decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_retract(decision_id: str, reason: str) -> str:
        """Retract a decision, marking it as no longer valid."""
        try:
            backend = get_backend()
            result = backend.decision_retract(decision_id, reason=reason)
            if result is None:
                return f"Decision not found: {decision_id}"
            return f"Decision retracted: {decision_id}"
        except Exception as e:
            logger.error(f"Failed to retract decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_reinforce(decision_id: str, evidence_id: str) -> str:
        """Record supporting evidence for a decision."""
        try:
            backend = get_backend()
            result = backend.decision_reinforce(decision_id, evidence_id)
            if result is None:
                return f"Decision not found: {decision_id}"

            return (
                f"Decision reinforced: {decision_id}\n"
                f"New confidence: {float(result.get('confidence') or 0.0):.2f}\n"
                f"Reinforcement count: {result.get('reinforcement_count')}"
            )
        except Exception as e:
            logger.error(f"Failed to reinforce decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_contradict(decision_id: str, evidence_id: str) -> str:
        """Record contradicting evidence against a decision."""
        try:
            backend = get_backend()
            result = backend.decision_contradict(decision_id, evidence_id)
            if result is None:
                return f"Decision not found: {decision_id}"

            return (
                f"Decision contradicted: {decision_id}\n"
                f"New confidence: {float(result.get('confidence') or 0.0):.2f}\n"
                f"Contradiction count: {result.get('contradiction_count')}"
            )
        except Exception as e:
            logger.error(f"Failed to contradict decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_provenance(decision_id: str) -> str:
        """Get full provenance chain for a decision."""
        try:
            backend = get_backend()
            provenance = backend.decision_provenance(decision_id)

            if not provenance or provenance.get("decision") is None:
                return f"Decision not found: {decision_id}"

            parts = [f"Provenance for {decision_id}:"]
            parts.append(f"Decision: {_field(provenance['decision'], 'content')}")
            if provenance.get("reasoning_trace"):
                parts.append(f"Reasoning trace: {provenance['reasoning_trace']}")
            if provenance.get("evidence"):
                parts.append(f"Evidence: {len(provenance['evidence'])} items")
            if provenance.get("superseded"):
                parts.append(f"Superseded: {len(provenance['superseded'])} decisions")
            if provenance.get("superseded_by"):
                parts.append(
                    f"Superseded by: {len(provenance['superseded_by'])} decisions"
                )
            return "\n".join(parts)
        except Exception as e:
            logger.error(f"Failed to get provenance: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_find_conflicts(decision_id: str) -> str:
        """Find existing decisions that may conflict with this one."""
        try:
            backend = get_backend()
            result = backend.decision_find_conflicts(decision_id)

            if result is None:
                return f"Decision not found: {decision_id}"

            conflicts = result.get("conflicts") or []
            if not conflicts:
                return f"No conflicts found for decision {decision_id}"

            output = [f"Found {len(conflicts)} conflicts for {decision_id}:\n"]
            for c in conflicts:
                output.append(
                    f"- [{_field(c, 'decision_id')}] ({_field(c, 'decision_type')}): "
                    f"{str(_field(c, 'content', ''))[:100]}"
                )
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Failed to find conflicts: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_create_pending(
        content: str,
        requirements: List[Dict[str, Any]],
        domain: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Create a pending decision (GTM Acceptance Case) with unresolved requirements.

        requirements: list of {description, requirement_type, query_hint?}. The
        server assigns each requirement_id (req_<uuid8>); use those with
        decision_resolve_requirement.
        """
        try:
            backend = get_backend()
            decision = backend.decision_create_pending(
                content=content,
                requirements=requirements,
                domain=domain,
                tags=tags or [],
            )
            lines = [
                f"Pending decision created: {_field(decision, 'decision_id')}",
                f"Status: {_field(decision, 'status')}",
                "Requirements:",
            ]
            for r in _field(decision, "pending_requirements") or []:
                lines.append(
                    f"  - {_field(r, 'requirement_id')}: {_field(r, 'description')} "
                    f"[{_field(r, 'requirement_type')}] resolved={_field(r, 'resolved')}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Failed to create pending decision: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_resolve_requirement(
        decision_id: str, requirement_id: str, memory_id: str
    ) -> str:
        """Mark a requirement on a pending decision resolved by a memory item."""
        try:
            backend = get_backend()
            ok = backend.decision_resolve_requirement(
                decision_id, requirement_id, memory_id
            )
            return (
                f"Requirement {requirement_id} resolved on {decision_id}"
                if ok
                else f"Requirement {requirement_id} NOT FOUND on {decision_id}"
            )
        except Exception as e:
            logger.error(f"Failed to resolve requirement: {e}", exc_info=True)
            raise

    @mcp.tool()
    @graceful
    def decision_try_activate(decision_id: str) -> str:
        """Activate a pending decision if all requirements are resolved (no-op otherwise)."""
        try:
            backend = get_backend()
            activated = backend.decision_try_activate(decision_id)
            return (
                f"Decision {decision_id} activated (pending -> active)"
                if activated
                else f"Decision {decision_id} still pending (unresolved requirements remain)"
            )
        except Exception as e:
            logger.error(f"Failed to activate pending decision: {e}", exc_info=True)
            raise
