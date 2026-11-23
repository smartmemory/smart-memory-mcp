"""Decision lifecycle MCP tools — create, query, supersede, retract, reinforce, contradict, link evidence.

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

from mcp.types import ToolAnnotations

from smartmemory_mcp.backends.interface import (
    DecisionAlreadyDecided,
    DecisionRestoreRefused,
    EvidenceRefused,
)
from .common import (
    checked_as_of,
    dated,
    get_backend,
    graceful,
    scoped_options,
    resolution_text,
    history_text,
    sites_text,
    who_text,
)

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


#: The lifecycle states decision_list reads (the service route's and core's ``status`` filter).
_LIST_STATES = ("active", "superseded", "retracted", "pending")


def _summary_line(decision: Any) -> str:
    """One list/search row. Shared so both tools cannot drift apart."""
    confidence = _field(decision, "confidence") or 0.0
    diagnostic = resolution_text(decision)  # once: it warns on malformed metadata
    return (
        f"- [{_field(decision, 'decision_id')}] "
        f"({_field(decision, 'decision_type')}, conf={confidence:.2f}): "
        f"{str(_field(decision, 'content', ''))[:100]}{_uncertainty_marker(decision)}{(' ' + diagnostic) if diagnostic else ''}"
    )


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _requirement_line(req: Any) -> str:
    resolved = _field(req, "resolved")
    done = resolved is True
    marker = f"[{'done' if done else 'open'}] " if isinstance(resolved, bool) else ""
    text = f"- {marker}{_field(req, 'description') or _field(req, 'requirement_id') or 'requirement'}"
    kind = _field(req, "requirement_type")
    if _present(kind):
        text += f" ({kind})"
    if done:
        by, at = _field(req, "resolved_by"), _field(req, "resolved_at")
        if _present(by):
            text += f", supporting memory {by}"  # resolved_by is an evidence memory id, not an actor
        if _present(at):
            text += f" on {at}"
    return text


def _decision_text(decision: Any, as_of: Optional[str] = None) -> str:
    """DECISION-GET-READABLE: one readable answer for a single decision read.

    Statement, state, rationale, lifecycle reason and requirements come first, then the shared who/replaced lines.
    Only what the authorized read returned is shown; an absent, None or empty field is omitted. Internal ids,
    counters and the raw context snapshot are never printed."""
    notes = _field(decision, "context_snapshot")
    notes = notes if isinstance(notes, dict) else {}
    head = f"Decision {_field(decision, 'decision_id')}"
    if as_of is not None:
        head += f" as of {as_of}"
    content = _field(decision, "content")
    lines = [f"{head}: {content}" if _present(content) else head]
    state = _field(decision, "display_status") or _field(decision, "status")
    if _present(state):
        extra = [str(x) for x in (_field(decision, "decision_type"),) if _present(x)]
        conf = _field(decision, "confidence")
        if isinstance(conf, (int, float)):
            extra.append(f"confidence {conf:.2f}")
        lines.append(f"State: {state}" + (f" ({', '.join(extra)})" if extra else "") + _uncertainty_marker(decision))
    if _present(_field(decision, "rationale")):
        lines.append(f"Rationale: {_field(decision, 'rationale')}")
    beliefs = [
        f"{name}={_field(decision, name):.2f}"
        for name in ("belief_hold", "plausibility_hold", "ignorance")
        if isinstance(_field(decision, name), (int, float))
    ]
    for label, reason_key, at_key in (
        ("Retracted", "retraction_reason", "retracted_at"),
        ("Replaced", "superseded_reason", "superseded_at"),
    ):
        reason, when = notes.get(reason_key), notes.get(at_key)
        if _present(reason):
            lines.append(f"{label}{f' on {when}' if _present(when) else ''}: {reason}")
    if beliefs:
        lines.append("Evidence: " + " ".join(beliefs))
    reqs = _field(decision, "pending_requirements")
    if _present(reqs):
        lines.append("Requirements:")
        lines.extend(_requirement_line(r) for r in reqs)
    for name, label in (("rejected_alternatives", "Rejected alternatives"), ("constraints", "Constraints")):
        if _present(_field(decision, name)):
            lines.append(f"{label}: {'; '.join(str(x) for x in _field(decision, name))}")
    who = "\n".join(x for x in who_text(decision).splitlines() if x != "Status: replaced")
    if who:
        lines.append(who)
    diagnostic = resolution_text(decision)
    if diagnostic:
        lines.append(diagnostic)
    unknown = _field(decision, "as_of_unknown_fields")
    if _present(unknown):
        lines.append(f"Not known at that instant: {', '.join(str(x) for x in unknown)}")
    return "\n".join(lines)


def _not_activated_text(backend: Any, decision_id: str) -> str:
    """ACTIVATE-REPLY: why a try_activate answered False, from one ordinary read (what the caller may read, no more).

    A staged record is not in the item store, so it reads as missing here; telling it apart needs a core signal
    (see units/BUG-MCP-ACTIVATE-REPLY-1/redesign-note.md)."""
    old = f"Decision {decision_id} still pending (unresolved requirements remain)"
    try:
        decision = backend.decision_get(decision_id)
    except Exception:
        return old
    if not decision:
        return f"Decision {decision_id} not found"
    status = _field(decision, "status")
    if status == "active":
        return f"Decision {decision_id} is already active"
    if status != "pending":
        return old
    open_reqs = [r for r in (_field(decision, "pending_requirements") or []) if not _field(r, "resolved")]
    shown = [r for r in open_reqs if _field(r, "visible") is not False]
    hidden = len(open_reqs) - len(shown)
    names = [str(_field(r, "description") or "unnamed requirement") for r in shown]
    if not names and not hidden:
        return old
    text = f"Decision {decision_id} still pending: waiting on " + "; ".join(names)
    if hidden:
        text += (" and " if names else "") + f"{hidden} more you cannot see"
    return text


def register(mcp):
    """Register decision tools with the MCP server (13 tools)."""

    @mcp.tool(
        title="Create a decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
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
        scope_go_lives: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Record a decision the user stated, with provenance.

        Use this when the user states a decision: "I decided X", "we chose X over
        Y", "we're going with X". Put the decision in `content` as one sentence and
        pass `rejected_alternatives` and `rationale` when the user gave them.
        scope_go_lives supplies explicit sponsor (scope=null) or exact site activation
        dates. A site falls back to the sponsor until an exact entry activates. A
        decision stored this way can later be reinforced, superseded or checked for
        conflicts; one stored with memory_save may not be.
        """
        options = scoped_options(scope_go_lives=scope_go_lives)
        # CORE-EXPERTISE-1 Phase 1: rejected_alternatives, rationale, constraints
        # capture the "why" structure that makes decisions usable expertise.
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
                **options,
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

    @mcp.tool(
        title="Get a decision",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_get(
        decision_id: str,
        as_of: Optional[str] = None,
        business_scope: Optional[Dict[str, Any]] = None,
        valid_at: Optional[str] = None,
    ) -> str:
        """Business validity uses business_scope (exact site) and valid_at.

        Exact activated site entries take precedence, otherwise use sponsor fallback.
        Omitted valid_at is resolved once at request start. as_of is independent
        transaction-time history.

        Retrieve a decision by ID.

        ``as_of`` (ISO 8601; omit for now): the decision as it stood then, its status
        from the supersession chain and version history; "not found" if it did not
        exist yet."""
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        as_of = checked_as_of(as_of)
        try:
            backend = get_backend()
            decision = backend.decision_get(decision_id, **dated(as_of), **options)

            if not decision:
                return f"Decision not found: {decision_id}"

            return _decision_text(decision, as_of)
        except Exception as e:
            logger.error(f"Failed to get decision: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="List active decisions",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_list(
        domain: Optional[str] = None,
        decision_type: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50,
        as_of: Optional[str] = None,
        business_scope: Optional[Dict[str, Any]] = None,
        valid_at: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """List decisions (rules) in one state: the ones in force by default, or the ones that
        were replaced or retracted.

        ``status``: omit or "active" for the rules in force now; "superseded" for rules that
        were replaced (answers "what did we use to charge?"); "retracted" for rules that were
        withdrawn; "pending" for rules waiting on a requirement. A replacement that is still
        waiting for a person is not a decision yet: it is listed by
        review_queue_list, and the rule it would replace stays "active" until a person settles it.

        ``as_of`` (ISO 8601; omit for now) lists the decisions that held that state at that
        instant: a decision superseded or retracted since is listed as active, one created
        later is not.

        Business validity uses business_scope (exact site) and valid_at. Exact activated site
        entries take precedence, otherwise use sponsor fallback. Omitted valid_at is resolved
        once at request start. as_of is independent transaction-time history."""
        if status is not None and status not in _LIST_STATES:
            return (
                f"status must be one of {', '.join(_LIST_STATES)} (omit it for active)"
            )
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        as_of = checked_as_of(as_of)
        try:
            backend = get_backend()
            decisions = backend.decision_list(
                domain=domain,
                decision_type=decision_type,
                min_confidence=min_confidence,
                limit=limit,
                **dated(as_of),
                **options,
                **({} if status in (None, "active") else {"status": status}),
            )

            when = "" if as_of is None else f" as of {as_of}"
            state = status or "active"
            if not decisions:
                return f"No {state} decisions found{when}."

            output = [f"Found {len(decisions)} {state} decisions{when}:\n"]
            output.extend(_summary_line(d) for d in decisions)
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Failed to list decisions: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="Search decisions",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_search(
        topic: str,
        limit: int = 20,
        as_of: Optional[str] = None,
        business_scope: Optional[Dict[str, Any]] = None,
        valid_at: Optional[str] = None,
    ) -> str:
        """Business validity uses business_scope (exact site) and valid_at.

        Exact activated site entries take precedence, otherwise use sponsor fallback.
        Omitted valid_at is resolved once at request start. as_of is independent
        transaction-time history.

        Search for active decisions related to a topic.

        ``as_of`` (ISO 8601; omit for now): the decisions active at that instant."""
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        as_of = checked_as_of(as_of)
        try:
            backend = get_backend()
            decisions = backend.decision_search(
                topic=topic, limit=limit, **dated(as_of), **options
            )

            when = "" if as_of is None else f" as of {as_of}"
            if not decisions:
                return f"No decisions found about: {topic}{when}"

            output = [f"Found {len(decisions)} decisions about '{topic}'{when}:\n"]
            output.extend(_summary_line(d) for d in decisions)
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Failed to search decisions: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="Supersede a decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
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
        scope_go_lives: Optional[List[Dict[str, Any]]] = None,
        valid_from: Optional[str] = None,
    ) -> str:
        """Replace a decision, preserving its rationale, alternatives and constraints.

        In a workspace that asks a person first (supersession policy "propose"), nothing is
        replaced yet: the answer says "held for review" with a review id and the old decision
        stays in force. A person settles it in the review queue: tell the user it is waiting,
        and do not settle it yourself. review_queue_list shows what is waiting. Sending the same
        change again after a person has decided it answers "already decided: <outcome> (review
        <id>)", not "held".

        scope_go_lives schedules exact site activation with sponsor fallback. A
        site-only amendment leaves the sponsor and other sites on their prior rule.
        valid_from is the legacy global date and cannot accompany scope_go_lives.
        """
        options = scoped_options(scope_go_lives=scope_go_lives, valid_from=valid_from)
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
                **options,
            )
            if result is None:
                return f"Decision not found: {decision_id}"

            if result.get("status") == "held_for_review":
                # OUTREACH-MARIUS-HITL-SURFACE-1 p2: the workspace holds replacements for a person (review.supersession
                # propose). Nothing was replaced. The assistant is never told to settle its own change: a person does,
                # in the review queue (OUTREACH-MARIUS-HITL-SURFACE-1 review M1).
                return (
                    f"Replacement of decision {decision_id} held for review (the workspace asks a person first).\n"
                    f"The old decision stays in force. Review: {result.get('review_id')}\n"
                    "Tell the user: the change is waiting for a person, who settles it in the review queue, and "
                    "the old rule applies until then. Do not say it was saved as the new rule, and do not settle "
                    "it yourself."
                )
            if result.get("status") == "scheduled":
                return (
                    f"Amendment scheduled for decision {decision_id}.\n"
                    f"New decision: {result['new_item_id']}\n"
                    f"scope_go_lives: {result['scope_go_lives']}"
                )
            return (
                f"Decision {decision_id} superseded.\n"
                f"New decision: {result.get('new_decision_id')}"
            )
        except DecisionAlreadyDecided as e:
            return f"already decided: {e.outcome} (review {e.review_id})"
        except Exception as e:
            logger.error(f"Failed to supersede decision: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="Retract a decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Restore a wrongly retired decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_restore(decision_id: str, reason: str) -> str:
        """Undo a wrong retirement in one action: the decision is active again, its replacement stays active, and the
        history keeps both. Only the decision's owner can restore it (DECISION-RESTORE-ONE-ACTION-1)."""
        try:
            backend = get_backend()
            result = backend.decision_restore(decision_id, reason=reason)
            if result is None:
                return f"Decision not found: {decision_id}"
            return (
                f"Decision restored: {decision_id}\n"
                f"Replacement stays active: {result.get('replacement_decision_id')}"
            )
        except DecisionRestoreRefused as e:
            newer = (
                f" (newer decision: {e.newer_decision_id})"
                if e.newer_decision_id
                else ""
            )
            return f"Restore refused ({e.code}): {e}{newer}"
        except Exception as e:
            logger.error(f"Failed to restore decision: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="Reinforce a decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Link evidence to a decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_link_evidence(decision_id: str, evidence_id: str) -> str:
        """Link a stored evidence receipt (its node id or `ev:` ref) to a live decision.

        Confidence is unchanged (use decision_reinforce to raise it); linking twice adds nothing.
        """
        try:
            backend = get_backend()
            result = backend.decision_link_evidence(decision_id, evidence_id)
        except EvidenceRefused as e:
            # A store refusal names its cause: decision/evidence not found, wrong type, ended decision.
            return f"Evidence not linked: {e}"
        except Exception as e:
            logger.error(f"Failed to link decision evidence: {e}", exc_info=True)
            raise

        return (
            f"Evidence linked: {result.get('evidence_id')} -> {decision_id}\n"
            f"Evidence count: {len(result.get('evidence_ids') or [])}\n"
            f"Confidence: {float(result.get('confidence') or 0.0):.2f} (unchanged)"
        )

    @mcp.tool(
        title="Record evidence",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def evidence_record(record: Dict[str, Any]) -> str:
        """Store one checked Compose evidence receipt (an `ev:` record) as an evidence memory.

        `record` is the Compose record as written: id (`ev:` + 16 hex), evidence (kind-specific, with
        `at` and `by`), checked_at, checks, and optional subject. Saving the same record again is a no-op.
        Link it to a decision with decision_link_evidence.
        """
        try:
            backend = get_backend()
            result = backend.evidence_record(record)
        except EvidenceRefused as e:
            return f"Evidence not recorded: {e}"
        except Exception as e:
            logger.error(f"Failed to record evidence: {e}", exc_info=True)
            raise

        return (
            f"Evidence recorded: {result.get('evidence_id')}\n"
            f"Ref: {result.get('evidence_ref')}\n"
            f"Kind: {result.get('kind')}\n"
            f"Checked at: {result.get('checked_at')}"
        )

    @mcp.tool(
        title="Contradict a decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Get decision provenance",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_provenance(
        decision_id: str,
        as_of: Optional[str] = None,
        business_scope: Optional[Dict[str, Any]] = None,
        valid_at: Optional[str] = None,
    ) -> str:
        """Business validity uses business_scope (exact site) and valid_at.

        Exact activated site entries take precedence, otherwise use sponsor fallback.
        Omitted valid_at is resolved once at request start. as_of is independent
        transaction-time history.

        Get full provenance chain for a decision.

        ``as_of`` (ISO 8601; omit for now): the provenance as it stood then; a
        successor that came later is not listed."""
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        as_of = checked_as_of(as_of)
        try:
            backend = get_backend()
            provenance = backend.decision_provenance(
                decision_id, **dated(as_of), **options
            )

            if not provenance or provenance.get("decision") is None:
                return f"Decision not found: {decision_id}"

            parts = [f"Provenance for {decision_id}:"]
            parts.append(f"Decision: {_field(provenance['decision'], 'content')}")
            if who := who_text(provenance["decision"]):
                parts.append(who)
            parts.append(
                f"Selected decision: {_field(provenance['decision'], 'decision_id')}"
            )
            if diagnostic := resolution_text(provenance["decision"]):
                parts.append(diagnostic)
            if provenance.get("reasoning_trace"):
                parts.append(f"Reasoning trace: {provenance['reasoning_trace']}")
            if provenance.get("evidence"):
                parts.append(f"Evidence: {len(provenance['evidence'])} items")
                # SM-EVIDENCE-TYPE-1: an evidence-node row names its receipt.
                for row in provenance["evidence"]:
                    if isinstance(row, dict) and row.get("evidence_ref"):
                        parts.append(
                            f"- {row['evidence_ref']} ({row.get('kind')}, checked {row.get('checked_at')})"
                        )
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

    @mcp.tool(
        title="Read a decision's history",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_history(
        decision_id: str,
        limit: Optional[int] = None,
        before: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> str:
        """A decision's dated history, newest first, in one call: recorded, edited, replaced, retracted, restored,
        review answers and evidence links, each with who did it and why. A replacement you cannot read shows as
        "a rule you cannot see", with no id or text.

        limit (1 to 500, default 100) and before (ISO 8601: only entries older than this) page through a long
        history. as_of (ISO 8601) keeps only entries at or before that instant."""
        as_of = checked_as_of(as_of)
        before = checked_as_of(before)
        try:
            backend = get_backend()
            history = backend.decision_history(
                decision_id, limit=limit, before=before, **dated(as_of)
            )
            if not history:
                return f"Decision not found: {decision_id}"
            return history_text(decision_id, history)
        except Exception as e:
            logger.error(f"Failed to read decision history: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="Which rule is in force at each site",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_sites(
        decision_id: str,
        at: Optional[str] = None,
        scopes: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """The whole site table of a decision in one call: which rule of its replacement chain is in force at each
        site on a date, and what starts next. A decision with no site schedule answers "All sites, from <date>".

        at (ISO 8601, default now) is the date asked about. scopes lists extra sites to answer for even when no rule
        names them, each {"site": "05"}: a site with nothing in force says so and is never filled with a default."""
        at = checked_as_of(at)
        # Each extra site passes the same check as a business_scope (exactly {"site": <name>}).
        checked = [
            scoped_options(business_scope=scope)["business_scope"]
            for scope in (scopes or [])
        ]
        try:
            backend = get_backend()
            sites = backend.decision_sites(decision_id, at=at, scopes=checked or None)
            if not sites:
                return f"Decision not found: {decision_id}"
            return sites_text(decision_id, sites)
        except Exception as e:
            logger.error(f"Failed to read decision sites: {e}", exc_info=True)
            raise

    @mcp.tool(
        title="Find decision conflicts",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Create a pending decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Resolve a decision requirement",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Activate a pending decision",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def decision_try_activate(decision_id: str) -> str:
        """Activate a pending decision if all requirements are resolved (no-op otherwise)."""
        try:
            backend = get_backend()
            activated = backend.decision_try_activate(decision_id)
            if activated:
                return f"Decision {decision_id} activated (pending -> active)"
            return _not_activated_text(backend, decision_id)
        except Exception as e:
            logger.error(f"Failed to activate pending decision: {e}", exc_info=True)
            raise
