"""Render the writer's stated attribution for one readable decision version."""


def attribution_text(decision) -> str:
    """Render only supplied names, keeping their status distinct from recorded actors."""
    lines = []
    for key, label in (
        ("recommended_by", "Recommended by"),
        ("decided_by", "Decided by"),
    ):
        name = (
            decision.get(key)
            if isinstance(decision, dict)
            else getattr(decision, key, None)
        )
        if isinstance(name, str) and name.strip():
            lines.append(f"{label} (as stated, not verified): {name.strip()}")
    return "\n".join(lines)


def with_attribution(text, decision) -> str:
    """Append attribution only when there is something to display."""
    attribution = attribution_text(decision)
    return f"{text}\n{attribution}" if attribution else text


def with_history_attribution(text, history, selected_id, backend, as_of):
    """Label the decisions visible on this page, independently of the audit-event actors."""
    from .common import dated, withheld_view

    visible_ids = [selected_id]
    for entry in history["entries"]:
        if entry.get("related_visible") and entry.get("related_decision_id"):
            visible_ids.append(entry["related_decision_id"])
    sections = [text]
    for ident in dict.fromkeys(visible_ids):
        decision = withheld_view(backend.decision_get(ident, **dated(as_of)))
        if (
            decision
            and decision.get("decision_id") == ident
            and (names := attribution_text(decision))
        ):
            sections.append(f"Decision {ident}:\n{names}")
    return "\n".join(sections)
