"""Server-level routing guidance sent to the model at MCP initialize (MCP-CAPTURE-ROUTING-1).

Without these, a model sees only per-tool docstrings and picks a write tool by its
title. The owner's 2026-10-17 report ("I decided..." and dated statements came back as
plain items) traced partly to that: no server instructions, a `memory_add` docstring
that was false on the hosted service, and a `decision_create` docstring that never said
when to use it. The two variants differ because the hosted allowlist has no decision
tools (`hosted/tools.py`), so pointing a hosted model at `decision_create` would send it
to a tool it cannot call.
"""

from __future__ import annotations

_COMMON = (
    "SmartMemory keeps what the user tells you so it can be recalled later.\n"
    "- Facts, events, plans and anything naming people, places, organizations or dates: "
    "use memory_ingest. It extracts entities, dates and the relations between them.\n"
)

LOCAL_INSTRUCTIONS = _COMMON + (
    "- A decision the user states ('I decided X', 'we chose X over Y', 'we are going with X'): "
    "use decision_create, with the decision as one sentence and "
    "rejected_alternatives / rationale when the user gave them.\n"
    "- memory_add in local mode stores the text as-is with no entity, date or decision "
    "extraction. Use it only for verbatim notes; prefer memory_ingest otherwise.\n"
)

HOSTED_INSTRUCTIONS = _COMMON + (
    "- memory_add runs the same extraction on this hosted service; use it when you need "
    "to attach metadata or tags.\n"
    "- This server has no decision tools. Record a decision with memory_ingest as one plain "
    "sentence that names the choice and what it replaced (for example 'Decided to use X "
    "instead of Y because Z') so it stays searchable.\n"
)
