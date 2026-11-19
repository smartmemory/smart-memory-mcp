"""STUDIO-DECISION-VIEW-1 U5, notes N2 and N17 through MCP in local mode.

Real Lite store (core ``create_lite_memory``), the local backend, the real tools. Needs core with the decision view
reads (``smartmemory.decisions.history_view``, core ``s203/dv-core-int``): skipped on an older core, never faked.
The acting person is answered the way the hosted service answers it (``get_acting_principal``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytest.importorskip("smartmemory.decisions.history_view")

D0 = "Each site may enroll at most 15 participants."
D1 = "Each site may enroll at most 20 participants."
REASON = "capacity review Q4"
SCHEDULE = [
    {"scope": {"site": "01"}, "valid_from": "2026-10-01T00:00:00Z"},
    {"scope": {"site": "03"}, "valid_from": "2026-11-01T00:00:00Z"},
]


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
    from smartmemory_mcp.tools import decision_tools

    for name in (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    config = PipelineConfig.lite(workspace_id="dv_ws", llm_enabled=False)
    config.enrich.enricher_names = []
    sm = create_lite_memory(data_dir=str(tmp_path), pipeline_profile=config, spawn_worker=False)
    sm._reranker_config.enabled = False
    policy = {"is_personal": False, "supersession_policy": "retire"}
    monkeypatch.setattr(
        sm.scope_provider, "get_workspace_supersession_policy", lambda: dict(policy), raising=False
    )
    current = {"actor": "human:bob"}
    monkeypatch.setattr(sm.scope_provider, "get_acting_principal", lambda: dict(current), raising=False)
    backend = LocalBackend()
    backend._mem = sm
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    try:
        yield sm, mcp.tools, current
    finally:
        sm.close()


def _seed(sm, current):
    from smartmemory.models.decision import Decision

    current["actor"] = "human:bob"
    d0 = sm.add_decision(
        D0,
        decided_by="protocol committee",
        rationale="pilot capacity",
        scope_go_lives=[{"scope": None, "valid_from": "2026-09-01T00:00:00Z"}],
    ).decision_id
    current["actor"] = "human:alice"
    with patch("smartmemory.decisions.manager.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 10, 1, tzinfo=timezone.utc)
        d1 = sm.supersede_decision(d0, Decision(content=D1), REASON, scope_go_lives=SCHEDULE).decision_id
    current["actor"] = "human:bob"
    return d0, d1


def test_n2_the_decision_read_says_who_recorded_what_replaced_it_who_when_why(world):
    sm, tools, current = world
    d0, d1 = _seed(sm, current)
    text = tools["decision_get"](decision_id=d0)
    assert "Recorded by: human:bob" in text
    assert f"Replaced by: {d1} on 2026-10-01" in text
    assert "by human:alice" in text and f"reason: {REASON}" in text
    assert "Decided by (as stated, not verified): protocol committee" in text
    # the provenance read answers the same in one call
    prov = tools["decision_provenance"](decision_id=d0)
    assert f"Replaced by: {d1} on 2026-10-01" in prov and "by human:alice" in prov and f"reason: {REASON}" in prov


def test_n17_every_site_in_one_call(world):
    sm, tools, current = world
    d0, d1 = _seed(sm, current)
    mid_oct = tools["decision_sites"](decision_id=d1, at="2026-10-15T00:00:00Z")
    line = {ln.split(":")[0].strip("- "): ln for ln in mid_oct.splitlines() if ln.startswith("- site")}
    assert d1 in line["site 01"] and "from 2026-10-01" in line["site 01"] and "next" not in line["site 01"]
    assert line["site 03"].startswith(f"- site 03: {d0} (from") and f"next: {d1} (from 2026-11-01" in line["site 03"]
    november = tools["decision_sites"](decision_id=d1, at="2026-11-02T00:00:00Z")
    rows = [ln for ln in november.splitlines() if ln.startswith("- site")]
    assert len(rows) == 2 and all(f": {d1} (from" in r and d0 not in r for r in rows)


def test_a_site_with_nothing_in_force_and_a_rule_with_no_schedule(world):
    sm, tools, current = world
    d0, d1 = _seed(sm, current)
    text = tools["decision_sites"](decision_id=d1, at="2026-10-15T00:00:00Z", scopes=[{"site": "05"}])
    assert "site 05" in text
    d2 = sm.add_decision("Screening visits need two staff present.").decision_id
    flat = tools["decision_sites"](decision_id=d2)
    assert flat.startswith("All sites, from ") and "- site" not in flat


def test_history_names_who_and_why(world):
    from smartmemory.models.decision import Decision

    sm, tools, current = world
    current["actor"] = "human:bob"
    d0 = sm.add_decision(D0).decision_id
    current["actor"] = "human:alice"
    d1 = sm.supersede_decision(d0, Decision(content=D1), REASON).decision_id
    text = tools["decision_history"](decision_id=d0)
    assert text.startswith(f"History of {d0} (newest first):")
    lines = text.splitlines()
    assert "replaced_by" in lines[1] and "by human:alice" in lines[1] and f"reason: {REASON}" in lines[1]
    assert f"(other decision: {d1})" in lines[1]
    assert "recorded" in text and "other decision: a rule you cannot see" not in text


def test_an_unknown_decision_is_not_found(world):
    _, tools, _ = world
    assert tools["decision_history"](decision_id="nope") == "Decision not found: nope"
    assert tools["decision_sites"](decision_id="nope") == "Decision not found: nope"
