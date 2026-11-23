"""BUG-MCP-DATED-SEARCH-REPLY-1: a dated decision_search names its instant, like decision_list."""

from unittest.mock import patch

from smartmemory.models.decision import Decision

from smartmemory_mcp.tools import decision_tools

AS_OF = "2020-01-15T00:00:00Z"


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Backend:
    def __init__(self, rows):
        self.rows = rows

    def decision_search(self, **kw):
        return self.rows


def _search(rows, **kw):
    mcp = _FakeMCP()
    decision_tools.register(mcp)
    with patch("smartmemory_mcp.tools.decision_tools.get_backend", return_value=_Backend(rows)):
        return mcp.tools["decision_search"](topic="kiosk", **kw)


def _row():
    return Decision(decision_id="dec_1", content="Blue case", confidence=0.9)


def test_d1_empty_dated_search_names_the_instant():
    assert _search([], as_of=AS_OF) == f"No decisions found about: kiosk as of {AS_OF}"


def test_d2_dated_search_with_rows_names_the_instant():
    first = _search([_row()], as_of=AS_OF).splitlines()[0]
    assert first == f"Found 1 decisions about 'kiosk' as of {AS_OF}:"


def test_d3_undated_empty_is_unchanged():
    assert _search([]) == "No decisions found about: kiosk"


def test_d4_undated_rows_header_is_unchanged():
    assert _search([_row()]).splitlines()[0] == "Found 1 decisions about 'kiosk':"
