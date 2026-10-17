"""MCP-HOSTED-DECISION-TOOLS-1 fix round 1: ids are one encoded path segment.

An id pasted raw into `/memory/decisions/{id}/conflicts` can rewrite the route: httpx
collapses `..` and a `?` cuts off the rest of the template, so `../../memory/clear?`
used to reach `POST /memory/clear` (review-r1 H1). Every by-id request method in
`smartmemory_mcp/` goes through `path_id`, and these tests pin that per method.
"""

from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from smartmemory_mcp.backends.remote import RemoteBackend, path_id
from smartmemory_mcp.tools import agent_tools

PACKAGE = Path(__file__).resolve().parents[1] / "smartmemory_mcp"
REMOTE = "backends/remote.py"
AGENT = "tools/agent_tools.py"

# Ids that used to change the route, plus an id that must stay one segment.
HOSTILE_IDS = ["../../memory/clear?", "a/b", "x#y", "%2e%2e", "ok-id_1"]
# Ids that name no single item: rejected before any request is made.
REJECTED_IDS = ["", ".", ".."]


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


def _agent(name, *args):
    def call(backend, item_id):
        mcp = _FakeMCP()
        agent_tools.register(mcp)
        return mcp.fns[name](item_id, *args)

    return call


# One row per by-id request site: (enclosing function, source file, HTTP method,
# route prefix, route suffix, call). The completeness guard below fails when a site
# in the package is missing from this table.
ROWS = [
    ("get", REMOTE, "GET", "/memory/", "", lambda b, i: b.get(i)),
    ("explain", REMOTE, "GET", "/memory/", "/explain", lambda b, i: b.explain(i)),
    ("update", REMOTE, "PATCH", "/memory/", "", lambda b, i: b.update(i, content="x")),
    ("delete", REMOTE, "DELETE", "/memory/", "", lambda b, i: b.delete(i)),
    (
        "decision_get",
        REMOTE,
        "GET",
        "/memory/decisions/",
        "",
        lambda b, i: b.decision_get(i),
    ),
    (
        "decision_supersede",
        REMOTE,
        "POST",
        "/memory/decisions/",
        "/supersede",
        lambda b, i: b.decision_supersede(i, "new", "why"),
    ),
    (
        "decision_retract",
        REMOTE,
        "POST",
        "/memory/decisions/",
        "/retract",
        lambda b, i: b.decision_retract(i, "why"),
    ),
    (
        "decision_reinforce",
        REMOTE,
        "POST",
        "/memory/decisions/",
        "/reinforce",
        lambda b, i: b.decision_reinforce(i, "ev"),
    ),
    (
        "decision_contradict",
        REMOTE,
        "POST",
        "/memory/decisions/",
        "/contradict",
        lambda b, i: b.decision_contradict(i, "ev"),
    ),
    (
        "decision_provenance",
        REMOTE,
        "GET",
        "/memory/decisions/",
        "/provenance",
        lambda b, i: b.decision_provenance(i),
    ),
    (
        "decision_find_conflicts",
        REMOTE,
        "POST",
        "/memory/decisions/",
        "/conflicts",
        lambda b, i: b.decision_find_conflicts(i),
    ),
    (
        "decision_resolve_requirement",
        REMOTE,
        "POST",
        "/memory/decisions/pending/",
        "/resolve",
        lambda b, i: b.decision_resolve_requirement(i, "req", "mem"),
    ),
    (
        "decision_try_activate",
        REMOTE,
        "POST",
        "/memory/decisions/pending/",
        "/activate",
        lambda b, i: b.decision_try_activate(i),
    ),
    (
        "agent_set_recall_profile",
        AGENT,
        "PUT",
        "/memory/agents/",
        "/recall-profile",
        _agent("agent_set_recall_profile", {"decision": 2.0}),
    ),
    (
        "agent_get_recall_profile",
        AGENT,
        "GET",
        "/memory/agents/",
        "/recall-profile",
        _agent("agent_get_recall_profile"),
    ),
]


@pytest.fixture
def wire(monkeypatch):
    """A RemoteBackend whose HTTP goes to a MockTransport that records requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "activated": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "smartmemory_mcp.backends.remote.httpx.request",
        lambda method, url, **kw: client.request(
            method, url, **{k: v for k, v in kw.items() if k != "timeout"}
        ),
    )
    backend = RemoteBackend(api_url="https://api.test", api_key="k", team_id="ws-1")
    backend._session["_bootstrapped"] = True
    monkeypatch.setattr(agent_tools, "get_backend", lambda: backend)
    return backend, seen


@pytest.mark.parametrize(
    "name,_file,method,prefix,suffix,call", ROWS, ids=[r[0] for r in ROWS]
)
def test_an_id_stays_one_encoded_segment_of_its_own_route(
    wire, name, _file, method, prefix, suffix, call
):
    backend, seen = wire
    for bad in HOSTILE_IDS:
        seen.clear()
        try:
            call(backend, bad)
        except ValueError:
            raise
        except Exception:
            pass  # the fake answer's shape is not what is under test, the request is
        assert len(seen) == 1, f"{name}({bad!r}) sent {len(seen)} requests"
        request = seen[0]
        want = f"{prefix}{quote(bad, safe='')}{suffix}"
        assert request.method == method
        assert request.url.raw_path.decode() == want, f"{name}({bad!r})"
        assert request.url.query == b"", f"{name}({bad!r}) grew a query string"

    for bad in REJECTED_IDS:
        seen.clear()
        with pytest.raises(ValueError):
            call(backend, bad)
        assert seen == [], f"{name}({bad!r}) sent a request"


def test_path_id_rejects_none_and_encodes_everything_else():
    with pytest.raises(ValueError):
        path_id(None)
    assert path_id("a/b?c#d e") == "a%2Fb%3Fc%23d%20e"
    assert path_id(42) == "42"


def _path_sites():
    """Every place in the package that builds a request path from a value.

    A path literal is a string starting with `/` and holding no whitespace. Sites are
    f-strings with a placeholder, `.format`, `%` and `+` on such a literal.
    """

    def is_path_literal(node):
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/")
            and not any(c.isspace() for c in node.value)
        )

    sites = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node.name
            return "<module>"

        for node in ast.walk(tree):
            rel = path.relative_to(PACKAGE).as_posix()
            if isinstance(node, ast.JoinedStr):
                parts = node.values
                texts = [p for p in parts if isinstance(p, ast.Constant)]
                if (
                    parts
                    and is_path_literal(parts[0])
                    and all(not any(c.isspace() for c in t.value) for t in texts)
                ):
                    holes = [
                        p.value for p in parts if isinstance(p, ast.FormattedValue)
                    ]
                    encoded = all(
                        isinstance(h, ast.Call)
                        and isinstance(h.func, ast.Name)
                        and h.func.id == "path_id"
                        for h in holes
                    )
                    sites.append((rel, enclosing(node), encoded))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and is_path_literal(node.func.value)
            ) or (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, (ast.Add, ast.Mod))
                and is_path_literal(node.left)
            ):
                sites.append((rel, enclosing(node), False))
    return sites


def test_every_id_in_a_request_path_goes_through_path_id_and_is_in_the_table():
    sites = _path_sites()
    unencoded = [(f, fn) for f, fn, ok in sites if not ok]
    assert unencoded == [], f"ids pasted raw into a request path: {unencoded}"
    found = {(f, fn) for f, fn, _ in sites}
    table = {(r[1], r[0]) for r in ROWS}
    assert found == table, (
        f"in the package but not the table: {sorted(found - table)}; "
        f"in the table but not the package: {sorted(table - found)}"
    )
    assert len(sites) == len(ROWS)
