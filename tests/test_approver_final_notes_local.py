"""OUTREACH-MARIUS-HITL-SURFACE-1 part 7 (2.0.2), approver final: the Lite half of the frozen notes.

Notes: smart-memory-ops ``units/OUTREACH-MARIUS-HITL-SURFACE-1/notes-approver-final.json`` (frozen before the code
change). Real Lite store (``create_lite_memory``), the real local MCP tool code, and the real ``smartmemory`` CLI for
the person's ``review-settings`` command. No code hook on the scope provider: the store's own settings row decides.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("smartmemory.decisions.held_supersession")

_spec = importlib.util.spec_from_file_location(
    "_decisions_connector_notes",
    Path(__file__).parent / "real_service" / "_decisions_connector_notes.py",
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _cli(data_dir, *args) -> str:
    from click.testing import CliRunner
    from smartmemory.cli import main

    result = CliRunner().invoke(
        main, ["--data-dir", str(data_dir), "review-settings", *args]
    )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def lite(tmp_path, monkeypatch):
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
    backend = LocalBackend()
    backend._mem = sm
    for module in (decision_tools, review_tools):
        monkeypatch.setattr(module, "get_backend", lambda: backend, raising=False)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    review_tools.register(mcp)
    try:
        yield sm, mcp.tools, tmp_path
    finally:
        sm.close()


def _open(sm, review_id) -> bool:
    return any(
        r["id"] == review_id
        for r in sm.list_conflict_reviews(status="open", limit=500, offset=0)
    )


def test_note1_lite_default_is_person_only(lite):
    _, w, data_dir = lite
    assert "who may approve: person_only (default)" in _cli(data_dir)
    view = json.loads(w["review_approval_get"]())
    assert view["policy"] == "person_only" and view["source"] == "default", view


def test_note7_lite_with_no_setting(lite):
    sm, w, data_dir = lite
    assert "hold for review: on" in _cli(data_dir, "--hold", "on")
    # Local get tools read the store's own settings, no "remote only".
    policy = w["supersession_policy_get"]()
    assert (
        "remote" not in policy.lower() and json.loads(policy)["policy"] == "propose"
    ), policy
    assert json.loads(w["review_approval_get"]())["policy"] == "person_only"

    old_id, _, new_text = base.seed_rule(w)
    _, first = base.hold(w, old_id, new_text)

    # The local tool's settle is refused, the text says how a person approves on a local store, the review stays open.
    out = w["review_queue_settle"](review_id=first, action="keep_b", reason="ok")
    assert "Only a person can approve reviews" in out, out
    assert (
        "actor='human:" in out
        and f"smartmemory --data-dir {data_dir} review-settings --approval anyone"
        in out
    ), out
    assert _open(sm, first)

    # The Python call that names a person settles.
    row = sm.settle_conflict_review(
        first, action="keep_b", reason="checked", actor="human:marius"
    )
    assert row["status"] == "resolved" and row["resolved_by"] == "human:marius", row

    # The local tool cannot set anyone, and names the command a person runs.
    out = w["review_approval_set"]("anyone")
    assert (
        out.startswith("Refused")
        and f"smartmemory --data-dir {data_dir} review-settings --approval anyone"
        in out
    ), out
    assert json.loads(w["review_approval_get"]())["policy"] == "person_only"

    # After the person runs the command, the local tool's settle works and is recorded as local:mcp.
    assert "who may approve: anyone" in _cli(data_dir, "--approval", "anyone")
    old2, _, new2 = base.seed_rule(w)
    _, second = base.hold(w, old2, new2)
    out = w["review_queue_settle"](review_id=second, action="keep_b", reason="ok")
    assert out.startswith("Settled:") and "local:mcp" in out, out
    assert not _open(sm, second)


def test_note9_local_list_says_how_many(lite):
    _, w, data_dir = lite
    _cli(data_dir, "--hold", "on")
    for _ in range(2):
        old_id, _, new_text = base.seed_rule(w)
        base.hold(w, old_id, new_text)
    assert w["review_queue_list"](limit=1).startswith("1 of 2 open conflict review(s)")
