"""BUG-PROVENANCE-REPLACED-NOT-FOUND-1: tracing a replaced decision on the public MCP path.

The user story: record rule A, replace it with rule B, ask ``decision_provenance`` for A. In 2.0.2 the local backend
answered "Decision not found" because the provenance read ran without the history gate. This pins the public path
(the real tool, never a direct ``DecisionQueries`` call) so it cannot come back:

* local leg: real Lite store (core ``create_lite_memory``), the real ``LocalBackend``, the real tool. This leg is
  where the history gate lives, so it is the one that fails if the gate is bypassed.
* hosted leg: the production hosted ASGI app over the wire, with ``svc-api`` stubbed at ``httpx.request`` (no live
  service). The stub answers the provenance route with the body the service returns for a replaced decision. This leg
  exercises the hosted tool dispatch, ``RemoteBackend`` route/params and rendering (a replaced decision is rendered,
  its successor named, never "Decision not found"). It does NOT exercise the service's history gate.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("smartmemory.decisions")

A = "Use Postgres for the ledger."
B = "Use MySQL for the ledger."
NOT_FOUND = "Decision not found"


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def local(tmp_path, monkeypatch):
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
    config = PipelineConfig.lite(workspace_id="prov_ws", llm_enabled=False)
    config.enrich.enricher_names = []
    sm = create_lite_memory(data_dir=str(tmp_path), pipeline_profile=config, spawn_worker=False)
    sm._reranker_config.enabled = False
    backend = LocalBackend()
    backend._mem = sm
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    try:
        yield sm, backend, mcp.tools
    finally:
        sm.close()


def _record_then_replace(sm):
    from smartmemory.models.decision import Decision

    a = sm.add_decision(A).decision_id
    b = sm.supersede_decision(a, Decision(content=B), "ledger review").decision_id
    return a, b


def _ids(rows):
    return [r["decision_id"] if isinstance(r, dict) else r.decision_id for r in rows]


def test_local_trace_of_a_replaced_decision_is_returned_not_not_found(local):
    sm, backend, tools = local
    a, b = _record_then_replace(sm)

    # the real tool, the way a client calls it
    text = tools["decision_provenance"](decision_id=a)
    assert NOT_FOUND not in text
    assert f"Provenance for {a}:" in text
    assert A in text
    assert "Superseded by: 1 decisions" in text

    # the backend answer behind it: A itself, replaced, successor B
    answer = backend.decision_provenance(a)
    assert answer is not None and answer["decision"] is not None
    assert answer["decision"]["decision_id"] == a
    assert _ids(answer["superseded_by"]) == [b]

    # and B, the current rule, names A as what it replaced
    current = tools["decision_provenance"](decision_id=b)
    assert NOT_FOUND not in current and B in current
    assert "Superseded: 1 decisions" in current
    assert _ids(backend.decision_provenance(b)["superseded"]) == [a]


def test_local_unknown_decision_is_still_not_found(local):
    _, _, tools = local
    assert tools["decision_provenance"](decision_id="nope") == "Decision not found: nope"


# --- hosted leg -------------------------------------------------------------------

REPLACED = {
    "decision_id": "dec-a",
    "content": A,
    "decision_type": "choice",
    "confidence": 0.8,
    "status": "superseded",
}
SUCCESSOR = {
    "decision_id": "dec-b",
    "content": B,
    "decision_type": "choice",
    "confidence": 0.8,
    "status": "active",
}


def test_hosted_trace_of_a_replaced_decision_is_returned_not_not_found(monkeypatch):
    from .test_hosted_tools_allowlist import _Harness, _text, fake_svc_api

    base = fake_svc_api()
    seen: list[tuple[str, str, dict]] = []

    def handler(method, url, **kwargs):
        path = httpx.URL(url).path
        if method == "GET" and path == "/memory/decisions/dec-a/provenance":
            seen.append((method, path, kwargs.get("params") or {}))
            body = {
                "decision": REPLACED,
                "reasoning_trace": None,
                "evidence": [],
                "superseded": [],
                "superseded_by": [SUCCESSOR],
            }
            return httpx.Response(200, json=body, request=httpx.Request(method, url))
        return base(method, url, **kwargs)

    monkeypatch.setattr("smartmemory_mcp.backends.remote.httpx.request", handler)
    harness = _Harness()

    async def body(client):
        session_id = await harness.session(client)
        return await harness.call(client, session_id, "decision_provenance", {"decision_id": "dec-a"})

    result = harness.run(body)
    text = _text(result)
    assert result.get("isError") is not True, text
    assert seen == [("GET", "/memory/decisions/dec-a/provenance", {})]
    assert NOT_FOUND not in text
    assert "Provenance for dec-a:" in text and A in text
    assert "Superseded by: 1 decisions" in text
