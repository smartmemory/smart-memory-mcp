"""BUG-DECISION-HISTORY-GAPS-1 r1: the MCP history text over a real Lite store (SQLite), no mock of a read or write.

M1: a review rejected by a different person names who answered, never the proposer; an old row that never stored
the answerer says "not recorded". L1: a replacement link older than any kept text says the text was not recorded.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from smartmemory_mcp.tools.common import history_text

SUPPORT = "Kestrel gets email support during business hours only."
SUPPORT_24 = "Kestrel gets 24/7 phone support."


@pytest.fixture
def lite(tmp_path, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    config = PipelineConfig.lite(workspace_id="hgaps_mcp_ws", llm_enabled=False)
    config.enrich.enricher_names = []
    sm = create_lite_memory(
        data_dir=str(tmp_path / f"lite_{uuid4().hex[:6]}"),
        pipeline_profile=config,
        spawn_worker=False,
    )
    sm._reranker_config.enabled = False
    policy = {"is_personal": False, "supersession_policy": "propose"}
    monkeypatch.setattr(
        sm.scope_provider,
        "get_workspace_supersession_policy",
        lambda: dict(policy),
        raising=False,
    )
    who: dict = {}
    monkeypatch.setattr(
        sm.scope_provider,
        "get_acting_principal",
        lambda: dict(who) if who else None,
        raising=False,
    )

    def sign_in(actor):
        who.clear()
        who["actor"] = actor

    try:
        yield sm, policy, sign_in
    finally:
        sm.close()


def _reject(sm, sign_in):
    from smartmemory.models.decision import Decision

    sign_in("human:sam")
    support = sm.add_decision(SUPPORT, decision_type="policy").decision_id
    held = sm.supersede_decision(
        support, Decision(content=SUPPORT_24), reason="Dana asked for it"
    )
    sign_in("human:maya")
    sm.settle_conflict_review(
        held.review_id, action="keep_a", reason="Not in the budget", actor="human:maya"
    )
    return support, held.review_id


def _line(text, action):
    [line] = [ln for ln in text.splitlines() if f" {action}" in ln]
    return line


def test_m1_a_rejection_names_who_answered_not_the_proposer(lite):
    sm, _, sign_in = lite
    support, _ = _reject(sm, sign_in)
    line = _line(
        history_text(support, sm.get_decision_history(support)), "review_rejected"
    )
    assert "answered by human:maya" in line and "asked by human:sam" in line
    assert "review_rejected by human:sam" not in line


def test_m1_an_old_row_without_the_answerer_says_not_recorded(lite):
    sm, _, sign_in = lite
    support, review_id = _reject(sm, sign_in)
    from smartmemory.reasoning.conflict_queue import ConflictReviewQueue

    queue = ConflictReviewQueue(sm._graph, initialize=False)
    with queue.backend._lock:
        (raw,) = queue.backend._conn.execute(
            "SELECT payload FROM conflict_reviews WHERE id=?", (review_id,)
        ).fetchone()
        row = json.loads(raw)
        row.pop(
            "resolved_by", None
        )  # the row shape from before the answerer was stored
        queue.backend._conn.execute(
            "UPDATE conflict_reviews SET payload=? WHERE id=?",
            (json.dumps(row), review_id),
        )
        queue.backend._conn.commit()
    line = _line(
        history_text(support, sm.get_decision_history(support)), "review_rejected"
    )
    assert "answered by not recorded" in line and "asked by human:sam" in line
    assert "review_rejected by human:sam" not in line


def test_l1_a_link_older_than_any_kept_text_says_not_recorded(lite):
    sm, policy, sign_in = lite
    policy["supersession_policy"] = "retire"
    sign_in("human:tom")
    old = sm.add_decision(
        "We will host on Netlify.", decision_type="policy"
    ).decision_id
    new = sm.add_decision(
        "We will host on Cloudflare Pages.", decision_type="policy"
    ).decision_id
    sm._graph.add_edge(
        source_id=new,
        target_id=old,
        edge_type="SUPERSESSION_UNDONE",
        properties={"superseded_at": "2024-03-01T09:00:00Z", "reason": "old plan"},
    )
    text = history_text(new, sm.get_decision_history(new))
    assert "was: text at the time not recorded" in text
    assert "We will host on Netlify." not in text
