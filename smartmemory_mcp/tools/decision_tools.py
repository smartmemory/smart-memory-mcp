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
from .as_of import with_as_of
from .common import (
    checked_as_of,
    dated,
    get_backend,
    graceful,
    scoped_options,
    resolution_text,
    withheld_view,
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


_ROW_CONTENT_MAX = 120


def _row_get(row: Any, name: str) -> Any:
    """One field off a row that may be a dict or an object; empty values read as absent."""
    value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    return value if value not in (None, "") else None


def _trim(text: Any) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _ROW_CONTENT_MAX else text[: _ROW_CONTENT_MAX - 1].rstrip() + "…"


def _link_for(selected: Any, side: str, row_id: Any) -> Any:
    """The visible supersession link of the selected decision that names ``row_id``.

    Core serves ``replaced`` (one link, to the successor) and ``replaces`` (links, to the predecessors) on the
    selected decision; each carries the id, reason and event time of that one transition."""
    links = _row_get(selected, side)
    if row_id is None or links is None:
        return None
    for link in links if isinstance(links, (list, tuple)) else [links]:
        if _row_get(link, "decision_id") == row_id and _row_get(link, "visible") is not False:
            return link
    return None


def _dated_successor_link(selected: Any, row_id: Any) -> Any:
    """A dated read is served without blocks, so no ``replaced`` link: the selected decision itself carries
    ``superseded_by`` (the successor id) and its folded note in ``context_snapshot``. Only that note, never the
    successor's own (it describes the successor's later change)."""
    if row_id is None or _row_get(selected, "superseded_by") != row_id:
        return None
    snapshot = _row_get(selected, "context_snapshot")
    if not isinstance(snapshot, dict):
        return None
    reason, when = _row_get(snapshot, "superseded_reason"), _row_get(snapshot, "superseded_at")
    return {"decision_id": row_id, "reason": reason, "at": when} if (reason or when) else None


def _decision_row_line(row: Any, link: Any = None, *, show_content: bool = True, own_note: bool = False) -> Optional[str]:
    """`- <id>: <content> (reason: <why>, <date>)`; a missing part is left out, never printed as None.

    Reason and date are those of the transition between the selected decision and this row: the ``link`` core
    returned for it, else (``own_note``, predecessors only) the row's own ``context_snapshot`` note, which describes
    the change that replaced it. Never the row's creation date."""
    ident = _row_get(row, "decision_id")
    content = _row_get(row, "content") if show_content else None
    if ident is None and content is None:
        return None
    reason = when = None
    if link is not None:
        reason, when = _row_get(link, "reason"), _row_get(link, "at")
    elif own_note:
        snapshot = _row_get(row, "context_snapshot")
        if isinstance(snapshot, dict):
            reason, when = _row_get(snapshot, "superseded_reason"), _row_get(snapshot, "superseded_at")
    head = " ".join(x for x in (f"{ident}:" if ident and content else ident, _trim(content) if content else None) if x)
    notes = [x for x in (f"reason: {_trim(reason)}" if reason else None, str(when) if when else None) if x]
    return f"- {head}" + (f" ({', '.join(notes)})" if notes else "")


def _created_of(obj: Any) -> Any:
    """A plain memory's creation time where core's dated filter looks: the row, its metadata, `metadata__created_at`."""
    when = _row_get(obj, "created_at")
    if when is None:
        metadata = _row_get(obj, "metadata")
        if metadata is not None:
            when = _row_get(metadata, "created_at")
    return when or _row_get(obj, "metadata__created_at")


def _evidence_row_line(row: Any, *, dated: bool = False) -> Optional[str]:
    # SM-EVIDENCE-TYPE-1: an evidence-node row names its receipt.
    ref = _row_get(row, "evidence_ref")
    checked = _row_get(row, "checked_at")
    if ref:
        kind = _row_get(row, "kind")
        notes = [x for x in (kind, f"checked {checked}" if checked else None) if x]
        return f"- {ref}" + (f" ({', '.join(str(n) for n in notes)})" if notes else "")
    memory = _row_get(row, "memory")
    content = _row_get(memory, "content") if memory is not None and not dated else None
    ident = _row_get(row, "memory_id") or (_row_get(memory, "item_id") if memory is not None else None)
    when = _created_of(memory) if memory is not None else None
    if content is None and ident is None:
        return None
    head = " ".join(x for x in (f"{ident}:" if ident and content else ident, _trim(content) if content else None) if x)
    # The creation date and the check time are different facts; keep both.
    dates = [x for x in (str(when) if when else None, f"checked {checked}" if checked else None) if x]
    return f"- {head}" + (f" ({', '.join(dates)})" if dates else "")


def _provenance_rows(provenance: Dict[str, Any], *, dated: bool = False) -> List[str]:
    """Evidence, superseded and successor sections: the count header, then one line per row.

    ``dated``: core hands back a neighbor's current wording on an as_of read (it is not the wording at that
    instant), so a dated line carries the id, reason and date only."""
    selected = provenance.get("decision")
    out: List[str] = []
    for key, header, unit, line in (
        ("evidence", "Evidence", "items", lambda r: _evidence_row_line(r, dated=dated)),
        (
            "superseded",
            "Superseded",
            "decisions",
            lambda r: _decision_row_line(
                r, _link_for(selected, "replaces", _row_get(r, "decision_id")), show_content=not dated, own_note=True
            ),
        ),
        (
            "superseded_by",
            "Superseded by",
            "decisions",
            lambda r: _decision_row_line(
                r,
                _link_for(selected, "replaced", _row_get(r, "decision_id"))
                # The snapshot stands in for the missing link only on a dated read (served without blocks); an
                # undated read whose link is hidden or names another id prints no transition.
                or (_dated_successor_link(selected, _row_get(r, "decision_id")) if dated else None),
                show_content=not dated,
            ),
        ),
    ):
        rows = provenance.get(key)
        if not rows:
            continue
        out.append(f"{header}: {len(rows)} {unit}")
        out.extend(text for row in rows if (text := line(row)))
    return out


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
    # The shared "Status: replaced" line is dropped only when the State line already says so. A decision replaced on a
    # site schedule stays ``active`` (still in force somewhere), and that line is the only place the read says it was
    # replaced (REL-2-0-5-POSTLAND-RED-NIGHT-1, decision-view note N2).
    said = str(state).lower() if _present(state) else ""
    who = "\n".join(
        x for x in who_text(decision).splitlines() if not (x == "Status: replaced" and said in ("replaced", "superseded"))
    )
    if who:
        lines.append(who)
    diagnostic = resolution_text(decision)
    if diagnostic:
        lines.append(diagnostic)
    unknown = _field(decision, "as_of_unknown_fields")
    if _present(unknown):
        lines.append(f"Not known at that instant: {', '.join(str(x) for x in unknown)}")
    return "\n".join(lines)


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
    @with_as_of
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

        Retrieve a decision by ID. ``as_of`` returns the decision as it stood then, its status
        from the supersession chain and version history; "not found" if it did not
        exist yet."""
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        try:
            backend = get_backend()
            decision = withheld_view(backend.decision_get(decision_id, **dated(as_of), **options))

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
    @with_as_of
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

        ``as_of`` lists the decisions that held that state at that
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
    @with_as_of
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

        ``as_of``: the decisions active at that instant."""
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
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
    @with_as_of
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

        ``as_of``: the provenance as it stood then; a
        successor that came later is not listed."""
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        try:
            backend = get_backend()
            provenance = withheld_view(
                backend.decision_provenance(decision_id, **dated(as_of), **options)
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
            parts.extend(_provenance_rows(provenance, dated=as_of is not None))
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
    @with_as_of
    @graceful
    def decision_history(
        decision_id: str,
        limit: Optional[int] = None,
        before: Optional[str] = None,
        as_of: Optional[str] = None,
        cursor: Optional[str] = None,
    ) -> str:
        """A decision's dated history, newest first, in one call: recorded, edited, replaced, retracted, restored,
        review answers and evidence links, each with who did it and why. A replacement you cannot read shows as
        "a rule you cannot see", with no id or text.

        limit (1 to 500, default 100) and cursor page through a long history: when the answer says more entries
        exist, pass the cursor it gives back as cursor to get the next page, with every entry shown exactly once.
        before (ISO 8601: only entries older than this) still works but is deprecated: it can skip entries that
        share one instant. as_of keeps only entries at or before that instant."""
        before = checked_as_of(before)
        try:
            backend = get_backend()
            history = withheld_view(
                backend.decision_history(
                    decision_id,
                    limit=limit,
                    before=before,
                    cursor=cursor,
                    **dated(as_of),
                )
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
            return (
                f"Decision {decision_id} activated (pending -> active)"
                if activated
                else f"Decision {decision_id} still pending (unresolved requirements remain)"
            )
        except Exception as e:
            logger.error(f"Failed to activate pending decision: {e}", exc_info=True)
            raise
