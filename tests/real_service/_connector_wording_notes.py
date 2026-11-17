"""OUTREACH-MARIUS-HITL-SURFACE-1 connector wording, the frozen notes as one driver both connector arms run.

Notes: smart-memory-ops ``units/OUTREACH-MARIUS-HITL-SURFACE-1/notes-connector-wording.json`` (frozen before the code
change). ``w`` is a tool set for a workspace that holds rule changes for a person (policy ``propose``): a mapping of
tool name to a callable taking keyword arguments and returning the tool's text, as the MCP client sees it. Loaded by
path by the local arm (tests/test_connector_wording_local.py) and the hosted arm
(tests/real_service/test_hosted_connector_wording.py).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_decisions_connector_notes",
    Path(__file__).with_name("_decisions_connector_notes.py"),
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

# What an assistant must never be told to do with its own held change.
SELF_APPROVE = re.compile(r"review_queue_settle|keep_[ab]|approv|accept", re.I)


def n_held(w) -> None:
    """N1 (local) and N2 (hosted): the held answer says a person settles it and gives the review id."""
    old_id, _, new_text = base.seed_rule(w)
    out, review_id = base.hold(w, old_id, new_text)
    low = out.lower()
    assert "held for review" in low and "person" in low, out
    assert "review queue" in low, out
    assert review_id in out, out
    assert "old" in low and "in force" in low, out
    assert not SELF_APPROVE.search(out), out
    assert "superseded." not in low, out


def _decide(w) -> tuple[str, str, str]:
    """Hold a change, a person rejects it, return (decision id, new text, review id)."""
    old_id, _, new_text = base.seed_rule(w)
    _, review_id = base.hold(w, old_id, new_text)
    out = w["review_queue_settle"](review_id=review_id, action="keep_a", reason="no")
    assert out.startswith("Settled:"), out
    return old_id, new_text, review_id


def n_already_decided(w) -> None:
    """N3 (local) and N4 (hosted): re-sending a settled change answers already decided, never held."""
    old_id, new_text, review_id = _decide(w)
    out = w["decision_supersede"](
        decision_id=old_id, new_content=new_text, reason="price change"
    )
    assert out.startswith("already decided: rejected"), out
    assert f"(review {review_id})" in out, out
    assert "held" not in out.lower(), out


def n_general_replace(w) -> None:
    """N5 (local) and N6 (hosted): memory_supersede on a decision shows the refusal as is."""
    old_id, _, new_text = base.seed_rule(w)
    out = w["memory_supersede"](item_id=old_id, content=new_text, reason="price change")
    assert isinstance(out, str), out
    assert "decision_supersede" in out and "files the review" in out, out
    assert "Nothing was written" in out, out
    assert "Superseded:" not in out, out
    assert "API error" not in out and "409" not in out, out


NOTES = (n_held, n_already_decided, n_general_replace)
