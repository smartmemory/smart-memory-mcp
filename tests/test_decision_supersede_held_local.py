"""OUTREACH-MARIUS-HITL-SURFACE-1 p2, note 4 through MCP: decision_supersede in a propose workspace answers "held".

Real Lite store (core ``create_lite_memory``), the local backend, the real tools. The store's scope provider answers
the workspace policy ``propose`` the way a host does (``get_workspace_supersession_policy``). The tool must say the
replacement is held with its review id, the old rule stays active, the local tool's settle is refused (a local tool is not a person), and a person's settle applies it.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("smartmemory.decisions.held_supersession")


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def world(tmp_path, monkeypatch):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    from smartmemory_mcp.backends.local import LocalBackend
    from smartmemory_mcp.tools import decision_tools, review_tools

    for name in (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    sm = create_lite_memory(
        data_dir=str(tmp_path),
        pipeline_profile=PipelineConfig.lite(llm_enabled=False),
        spawn_worker=False,
    )
    policy = {"is_personal": False, "supersession_policy": "propose"}
    monkeypatch.setattr(
        sm.scope_provider,
        "get_workspace_supersession_policy",
        lambda: dict(policy),
        raising=False,
    )
    backend = LocalBackend()
    backend._mem = sm
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    monkeypatch.setattr(review_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    review_tools.register(mcp)
    try:
        yield sm, mcp.tools, policy
    finally:
        sm.close()


def _active(sm):
    return sorted(d.content for d in sm.get_active_decisions(limit=50))


def test_decision_supersede_is_held_and_settle_applies_it(world):
    sm, tools, _ = world
    old = sm.add_decision("We charge $40 a seat.", decision_type="policy").decision_id
    text = tools["decision_supersede"](
        decision_id=old, new_content="We now charge $45 a seat.", reason="price change"
    ).content[0].text
    assert "held for review" in text and "superseded." not in text
    review_id = re.search(r"Review: (conflict_[0-9a-f]+)", text).group(1)
    assert _active(sm) == ["We charge $40 a seat."]
    assert review_id in tools["review_queue_list"]()
    # The local MCP tool records the actor ``local:mcp``, which is not a person: under the default
    # ``review.approval`` (person_only, part 7) it is refused before anything is written.
    refused = tools["review_queue_settle"](
        review_id=review_id, action="keep_b", reason="approved"
    )
    assert refused.startswith("Refused: Only a person can approve reviews")
    assert "person_only" in refused and "a local tool" in refused
    assert review_id in tools["review_queue_list"]()
    assert _active(sm) == ["We charge $40 a seat."]
    # A person settles on a local store with a call that names them (the refusal's own hint).
    sm.settle_conflict_review(review_id, action="keep_b", reason="approved", actor="human:owner")
    assert _active(sm) == ["We now charge $45 a seat."]
    assert review_id not in tools["review_queue_list"]()


def test_retire_workspace_still_supersedes_at_once(world):
    sm, tools, policy = world
    policy["supersession_policy"] = "retire"
    old = sm.add_decision("We charge $40 a seat.", decision_type="policy").decision_id
    text = tools["decision_supersede"](
        decision_id=old, new_content="We now charge $45 a seat.", reason="price change"
    ).content[0].text
    assert text.startswith(f"Decision {old} superseded.")
    assert _active(sm) == ["We now charge $45 a seat."]
