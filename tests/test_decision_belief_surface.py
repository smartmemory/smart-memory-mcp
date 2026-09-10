"""CORE-DECISION-BELIEF-SURFACE-1 — the belief reads reach the MCP surface.

``decision_get`` returns ``Decision.to_dict()`` verbatim, so it inherits
``belief_hold`` / ``plausibility_hold`` / ``ignorance`` with no code change. That is
exactly the kind of thing a later refactor drops silently, so it is pinned here.

``decision_list`` / ``decision_search`` render a fixed one-line summary that would not
show a new dict key at all, so they gain a words-not-maths uncertainty marker: an agent
scanning the list must not read an unevidenced decision as a settled assertion just
because its prior ``confidence`` is high.

Local mode only. The remote-backend refusal (``_REMOTE_DECISION_MSG``) is unchanged and
out of scope for this feature.
"""

from unittest.mock import patch

from smartmemory.models.decision import Decision


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tools():
    from smartmemory_mcp.tools import decision_tools

    mcp = _FakeMCP()
    decision_tools.register(mcp)
    return mcp.tools


class _LocalBackend:
    _mem = object()


def _decision(decision_id: str, *, supports: int = 0, contradicts: int = 0) -> Decision:
    d = Decision(decision_id=decision_id, content="Use JWT for auth", confidence=0.9)
    for i in range(supports):
        d.reinforce(f"e{i}")
    for i in range(contradicts):
        d.contradict(f"x{i}")
    return d


def _run(tool_name, tools, manager=None, queries=None, **kwargs):
    patches = [patch("smartmemory_mcp.tools.decision_tools.get_backend")]
    if manager is not None:
        patches.append(patch("smartmemory.decisions.manager.DecisionManager", manager))
    if queries is not None:
        patches.append(patch("smartmemory.decisions.queries.DecisionQueries", queries))
    started = [p.start() for p in patches]
    try:
        started[0].return_value = _LocalBackend()
        return tools[tool_name](**kwargs)
    finally:
        for p in patches:
            p.stop()


def test_decision_get_returns_the_three_belief_reads():
    evidenced = _decision("dec_get", supports=4, contradicts=1)

    class _FakeManager:
        def __init__(self, mem):
            pass

        def get_decision(self, decision_id):
            return evidenced

    out = _run("decision_get", _tools(), manager=_FakeManager, decision_id="dec_get")

    for key in ("belief_hold", "plausibility_hold", "ignorance"):
        assert key in out, f"{key} missing from decision_get output: {out!r}"


def test_decision_get_belief_values_match_the_model():
    vacuous = _decision("dec_vac")

    class _FakeManager:
        def __init__(self, mem):
            pass

        def get_decision(self, decision_id):
            return vacuous

    out = _run("decision_get", _tools(), manager=_FakeManager, decision_id="dec_vac")

    payload = eval(out)  # noqa: S307 - decision_get renders a dict repr
    assert payload["ignorance"] == 1.0
    assert payload["belief_hold"] == 0.0
    assert payload["confidence"] == 0.9, "the prior is untouched by the evidence read"


def test_decision_list_marks_an_unevidenced_decision_in_words():
    """The 0.9 confidence must not read as settled when nothing has evidenced it."""
    unevidenced = _decision("dec_unev")

    class _FakeQueries:
        def __init__(self, mem):
            pass

        def get_active_decisions(self, **kw):
            return [unevidenced]

    out = _run("decision_list", _tools(), queries=_FakeQueries)

    assert "not enough evidence" in out
    assert "dec_unev" in out
    assert "1.0" not in out.split("dec_unev")[1], "the marker is words, not a float"


def test_decision_list_leaves_a_well_evidenced_decision_unmarked():
    evidenced = _decision("dec_ev", supports=5)

    class _FakeQueries:
        def __init__(self, mem):
            pass

        def get_active_decisions(self, **kw):
            return [evidenced]

    out = _run("decision_list", _tools(), queries=_FakeQueries)

    assert "dec_ev" in out
    assert "not enough evidence" not in out


def test_decision_search_marks_uncertainty_the_same_way():
    unevidenced = _decision("dec_search")

    class _FakeQueries:
        def __init__(self, mem):
            pass

        def get_decisions_about(self, **kw):
            return [unevidenced]

    out = _run("decision_search", _tools(), queries=_FakeQueries, topic="auth")

    assert "not enough evidence" in out
    assert "dec_search" in out


def test_the_marker_threshold_is_the_contracted_value():
    from smartmemory_mcp.tools import decision_tools

    assert decision_tools._UNCERTAINTY_THRESHOLD == 0.7
