"""OUTREACH-MARIUS-HITL-SURFACE-1 part 4, the frozen notes against the LOCAL connector on real Lite stores.

Two real Lite stores (core ``create_lite_memory``): W1 answers the workspace policy ``propose`` the way a host does,
W2 is a second, separate store. The real decision and review tools run on the local backend. A local settle is
recorded as ``local:mcp``. Driver and notes: ``tests/real_service/_decisions_connector_notes.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("smartmemory.decisions.held_supersession")

_spec = importlib.util.spec_from_file_location(
    "_decisions_connector_notes",
    Path(__file__).parent / "real_service" / "_decisions_connector_notes.py",
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


def _lite(path, policy):
    from smartmemory.pipeline.config import PipelineConfig
    from smartmemory.tools.factory import create_lite_memory

    sm = create_lite_memory(
        data_dir=str(path),
        pipeline_profile=PipelineConfig.lite(llm_enabled=False),
        spawn_worker=False,
    )
    sm.scope_provider.get_workspace_supersession_policy = lambda: {
        "is_personal": False,
        "supersession_policy": policy,
    }
    # Part 7 (2.0.2): the default approver is a person, and these notes settle through the local tool (local:mcp),
    # so the store is set to anyone, the way a person running the review-settings command would.
    sm.scope_provider.get_workspace_review_approval = lambda: "anyone"
    return sm


@pytest.fixture
def arms(tmp_path, monkeypatch):
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
    stores = [_lite(tmp_path / "w1", "propose"), _lite(tmp_path / "w2", "propose")]
    current: dict = {}
    monkeypatch.setattr(decision_tools, "get_backend", lambda: current["b"])
    monkeypatch.setattr(review_tools, "get_backend", lambda: current["b"])
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    review_tools.register(mcp)

    def toolset(sm):
        backend = LocalBackend()
        backend._mem = sm

        def bind(fn):
            def call(**kwargs):
                current["b"] = backend
                return fn(**kwargs)

            return call

        return {name: bind(fn) for name, fn in mcp.tools.items()}

    try:
        yield toolset(stores[0]), toolset(stores[1])
    finally:
        for sm in stores:
            sm.close()


@pytest.mark.parametrize("note", notes.NOTES, ids=lambda n: n.__name__)
def test_note_on_the_local_connector(arms, note):
    w1, w2 = arms
    note(w1, w2, lambda settled: "by local:mcp" in settled)
