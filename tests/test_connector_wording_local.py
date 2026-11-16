"""OUTREACH-MARIUS-HITL-SURFACE-1 connector wording on the LOCAL connector, real Lite store and the real tool code.

The store answers the workspace policy ``propose`` the way a host does. Driver and notes:
``tests/real_service/_connector_wording_notes.py``. Notes 7 and 8 read the tool descriptions the assistant is shown.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytest.importorskip("smartmemory.decisions.held_supersession")

_spec = importlib.util.spec_from_file_location(
    "_connector_wording_notes",
    Path(__file__).parent / "real_service" / "_connector_wording_notes.py",
)
notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notes)


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
    from smartmemory_mcp.tools import (
        decision_tools,
        memory_tools,
        review_tools,
        save_tools,
    )

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
    sm.scope_provider.get_workspace_supersession_policy = lambda: {
        "is_personal": False,
        "supersession_policy": "propose",
    }
    # Part 7 (2.0.2): the default approver is a person, and these notes settle through the local tool (local:mcp),
    # so the store is set to anyone, the way a person running the review-settings command would.
    sm.scope_provider.get_workspace_review_approval = lambda: "anyone"
    backend = LocalBackend()
    backend._mem = sm
    for module in (decision_tools, review_tools, memory_tools, save_tools):
        monkeypatch.setattr(module, "get_backend", lambda: backend, raising=False)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    review_tools.register(mcp)
    memory_tools.register_free(mcp)
    memory_tools.register_pro(mcp)
    save_tools.register_free(mcp)
    try:
        yield mcp.tools
    finally:
        sm.close()


@pytest.mark.parametrize("note", notes.NOTES, ids=lambda n: n.__name__)
def test_note_on_the_local_connector(world, note):
    note(world)


def test_n7_the_settle_tool_says_a_person_decides(world):
    settle = (world["review_queue_settle"].__doc__ or "").lower()
    assert "person" in settle and "refused" in settle, settle
    listing = world["review_queue_list"].__doc__ or ""
    assert not re.search(r"approve or reject", listing, re.I), listing


def test_n7_no_description_sends_the_agent_to_settle_its_own_change(world):
    for name in ("decision_supersede", "review_queue_list"):
        doc = world[name].__doc__ or ""
        assert not notes.SELF_APPROVE.search(doc), (name, doc)


def test_n8_no_description_points_to_memory_supersede_for_a_rule(world):
    for name in ("memory_update", "memory_save", "memory_supersede"):
        doc = world[name].__doc__ or ""
        for sentence in re.split(r"(?<=\.)\s+", doc):
            if "rule" in sentence and "memory_supersede" in sentence:
                assert "decision_supersede" in sentence, (name, sentence)
    assert "decision_supersede" in (world["memory_supersede"].__doc__ or "")
