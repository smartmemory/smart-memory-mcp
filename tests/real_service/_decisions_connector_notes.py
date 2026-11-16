"""The frozen real-use notes of OUTREACH-MARIUS-HITL-SURFACE-1 part 4, as one driver both connector arms run.

Notes: smart-memory-ops ``units/OUTREACH-MARIUS-HITL-SURFACE-1/notes-decisions-connector.json`` (frozen before the
code change). Each note is what a person says to an assistant; the driver makes the tool calls the assistant makes and
checks the answer in plain code. ``w1`` is a propose workspace, ``w2`` a second workspace of another owner. A tool set
is a mapping of tool name to a callable taking keyword arguments and returning the tool's text, exactly as the MCP
client would see it. ``actor_ok(settled_text)`` is the arm's rule for who a settle is recorded as.

The local arm (tests/test_decisions_connector_notes_local.py) and the hosted arm
(tests/real_service/test_hosted_decisions_connector_notes.py) load this file by path, so it imports nothing from a
``tests`` package.
"""

from __future__ import annotations

import re
import uuid

STATES_HINT = "active, superseded, retracted, pending"


def tag() -> str:
    return uuid.uuid4().hex[:8]


def create(tools, text: str) -> str:
    out = tools["decision_create"](content=text, decision_type="policy", confidence=0.9)
    found = re.search(r"Decision created: (\S+)", out)
    assert found, out
    return found.group(1)


def hold(tools, old_id: str, new_text: str) -> tuple[str, str]:
    out = tools["decision_supersede"](
        decision_id=old_id, new_content=new_text, reason="price change"
    )
    found = re.search(r"Review: (\S+)", out)
    assert found, out
    return out, found.group(1)


def listed(tools, **kwargs) -> str:
    return tools["decision_list"](limit=200, **kwargs)


def seed_rule(tools) -> tuple[str, str, str]:
    t = tag()
    old_text, new_text = (
        f"We charge $40 a seat. ({t})",
        f"We now charge $45 a seat. ({t})",
    )
    return create(tools, old_text), old_text, new_text


def note1(w1, w2, actor_ok) -> None:
    """'We now charge $45 a seat.' -> waiting for a person, old rule in force, what to tell the user."""
    old_id, old_text, new_text = seed_rule(w1)
    out, review_id = hold(w1, old_id, new_text)
    assert "held for review" in out and "stays in force" in out, out
    assert "Tell the user" in out and "person" in out, out
    now = listed(w1)
    assert old_text in now and new_text not in now, now


def note2(w1, w2, actor_ok) -> None:
    """'What's waiting for approval?' -> the open review, both sides named."""
    old_id, old_text, new_text = seed_rule(w1)
    _, review_id = hold(w1, old_id, new_text)
    out = w1["review_queue_list"]()
    assert review_id in out and old_text in out and new_text in out, out


def note3(w1, w2, actor_ok) -> None:
    """'Approve it.' -> the new rule in force, recorded as the right caller."""
    old_id, old_text, new_text = seed_rule(w1)
    _, review_id = hold(w1, old_id, new_text)
    out = w1["review_queue_settle"](
        review_id=review_id, action="keep_b", reason="approved"
    )
    assert out.startswith("Settled:") and "keep_b" in out, out
    assert actor_ok(out), out
    now = listed(w1)
    assert new_text in now and old_text not in now, now


def note4(w1, w2, actor_ok) -> None:
    """'No, reject it.' -> the old rule stays, no $45 rule in force."""
    old_id, old_text, new_text = seed_rule(w1)
    _, review_id = hold(w1, old_id, new_text)
    out = w1["review_queue_settle"](
        review_id=review_id, action="keep_a", reason="not approved"
    )
    assert out.startswith("Settled:") and "keep_a" in out, out
    now = listed(w1)
    assert old_text in now and new_text not in now, now


def accepted(w1) -> tuple[str, str]:
    old_id, old_text, new_text = seed_rule(w1)
    _, review_id = hold(w1, old_id, new_text)
    out = w1["review_queue_settle"](
        review_id=review_id, action="keep_b", reason="approved"
    )
    assert out.startswith("Settled:"), out
    return old_text, new_text


def retracted(w1) -> str:
    text = f"Refunds within 14 days. ({tag()})"
    rid = create(w1, text)
    out = w1["decision_retract"](decision_id=rid, reason="policy dropped")
    assert "retracted" in out, out
    return text


def note5(w1, w2, actor_ok) -> None:
    """'What did we use to charge?' -> the replaced rule, not the one in force."""
    old_text, new_text = accepted(w1)
    out = listed(w1, status="superseded")
    assert old_text in out and new_text not in out, out


def note6(w1, w2, actor_ok) -> None:
    """'Show me what was retracted.' -> the retracted rule only; the default list hides it."""
    text = retracted(w1)
    in_force = f"Invoices are due in 30 days. ({tag()})"
    create(w1, in_force)
    out = listed(w1, status="retracted")
    assert text in out and in_force not in out, out
    assert text not in listed(w1), "the default list must not show a retracted rule"


def note7(w1, w2, actor_ok) -> None:
    """A second workspace's assistant sees none of it and cannot settle W1's review."""
    old_text, new_text = accepted(w1)
    gone = retracted(w1)
    old_id, waiting_old, waiting_new = seed_rule(w1)
    _, review_id = hold(w1, old_id, waiting_new)
    w2_reviews = w2["review_queue_list"]()
    assert review_id not in w2_reviews and waiting_new not in w2_reviews, w2_reviews
    for state in ("superseded", "retracted"):
        out = listed(w2, status=state)
        for text in (old_text, new_text, gone, waiting_old, waiting_new):
            assert text not in out, (state, out)
    refused = w2["review_queue_settle"](
        review_id=review_id, action="keep_b", reason="approved"
    )
    assert "not found" in refused.lower() or "404" in refused, refused
    still = w1["review_queue_list"]()
    assert review_id in still, still
    assert waiting_old in listed(w1)


def note8(w1, w2, actor_ok) -> None:
    """A state that does not exist is refused plainly, naming the allowed ones."""
    out = listed(w1, status="deleted")
    assert STATES_HINT in out and not out.startswith("No "), out


NOTES = (note1, note2, note3, note4, note5, note6, note7, note8)
