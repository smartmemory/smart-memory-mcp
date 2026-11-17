"""OUTREACH-MARIUS-HITL-SURFACE-1 part 8 (2.0.2), key approve: the Lite half (note 12) of the frozen notes.

Notes: smart-memory-ops ``units/OUTREACH-MARIUS-HITL-SURFACE-1/notes-key-approve.json`` (frozen before the code
change). Lite has no API keys, so it keeps ``person_only`` as its unset default; ``person_or_key`` is accepted as a
setting and the local MCP tool (``local:mcp``, not a key) is still refused under it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("smartmemory.decisions.held_supersession")

_spec = importlib.util.spec_from_file_location(
    "_approver_final_notes_local",
    Path(__file__).parent / "test_approver_final_notes_local.py",
)
af = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(af)
base, _cli, _open = af.base, af._cli, af._open
lite = af.lite


def test_note12_lite_behaves_as_before(lite):
    sm, w, data_dir = lite
    assert "who may approve: person_only (default)" in _cli(data_dir)
    _cli(data_dir, "--hold", "on")
    old_id, _, new_text = base.seed_rule(w)
    _, first = base.hold(w, old_id, new_text)
    out = w["review_queue_settle"](review_id=first, action="keep_b", reason="ok")
    assert "Only a person can approve reviews" in out, out
    assert _open(sm, first)
    row = sm.settle_conflict_review(
        first, action="keep_b", reason="checked", actor="human:marius"
    )
    assert row["status"] == "resolved" and row["resolved_by"] == "human:marius", row

    assert "who may approve: person_or_key" in _cli(
        data_dir, "--approval", "person_or_key"
    )
    assert json.loads(w["review_approval_get"]())["policy"] == "person_or_key"
    old2, _, new2 = base.seed_rule(w)
    _, second = base.hold(w, old2, new2)
    out = w["review_queue_settle"](review_id=second, action="keep_b", reason="ok")
    assert "Only a person or an API key can approve reviews" in out, out
    assert _open(sm, second)
