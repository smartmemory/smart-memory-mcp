"""MCP-HOSTED-DECISION-TOOLS-1: an id in a request path reaches only its own route.

Fix round 1 percent-encoded every id. That was not enough (review-r2): the service runs
uvicorn, which unquotes the raw path before Starlette routes it, so `teams%2FT1` routes
as `teams/T1` and `memory_delete("teams/T1")` reached `DELETE /memory/teams/{team_id}`.
A plain id equal to a sibling route (`clear-all`) needed no encoding trick at all.

These tests assert where the SERVICE routes each request: the URL the real
`RemoteBackend` sends is decoded the way uvicorn decodes it and matched against the
service route table, and the only template it may match is the intended one. An id
that could go anywhere else must be refused before any request is sent.

Route table: the checked-in `tests/fixtures/service_route_table.json`, generated from
smart-memory-service origin/2.0 (its `_source` line names the sha). When
SMARTMEMORY_SERVICE_CHECKOUT points at a service checkout, the table is read from that
checkout's snapshot instead and `test_route_table_fixture_matches_service_snapshot`
requires the fixture to equal it. Regenerate with
`SMARTMEMORY_SERVICE_CHECKOUT=<2.0 checkout> python3 tests/test_path_id_encoding.py --write-route-table`.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from smartmemory_mcp.backends.path_guard import RESERVED_SEGMENTS, path_id
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.tools import agent_tools, decision_tools, memory_tools

HERE = Path(__file__).resolve()
PACKAGE = HERE.parents[1] / "smartmemory_mcp"
FIXTURE = HERE.parent / "fixtures" / "service_route_table.json"
SNAPSHOT_REL = "smart-memory-service/tests/contracts/snapshots/openapi_schema.json"
REMOTE = "backends/remote.py"
AGENT = "tools/agent_tools.py"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Service params declared with Starlette's `:path` convertor (regex `.*`, crosses `/`).
# OpenAPI drops the convertor, so it is listed here; the snapshot test re-derives it
# from the service route sources when they are present.
PATH_CONVERTORS = {
    ("GET", "/memory/archive/{archive_uri}"),
    ("GET", "/memory/ontology/templates/{template_id}"),
    ("DELETE", "/memory/ontology/templates/{template_id}"),
}

# review-r2's 12 probe ids, the brief's additions, and a few more of the class.
HOSTILE_IDS = [
    "../../health?",
    "a/b",
    "../x",
    "%2e%2e",
    "x?y=1",
    "%2e%2e/",
    "#frag",
    "teams/T1",
    "agents/A1",
    "api-keys/K1",
    "workspaces/W1/share/T1",
    "pipeline/P1",
    "clear-all",
    "api-keys",
    "teams",
    "..",
    ".",
    "",
    "a\\b",
    "%2F",
    "a%2Fb",
    "a\nb",
    "a\x00b",
    "pending",
    "search",
    "create",
    "archive",
    "decisions",
]
# Real id shapes: a uuid, a memory id, a decision id (`dec_` + 12 hex, core
# models/decision.py). They must go out byte-identical to 9454dc7 (no encoding).
NORMAL_IDS = ["3f2b6c1e-8a4d-4f7b-9c2e-1a2b3c4d5e6f", "mem_abc123", "dec_3f2b6c1e8a4d"]


def _find_snapshot() -> Path | None:
    # Only an explicit checkout (the same override as tests/test_hosted_route_contract.py).
    # No sibling auto-discovery: the sibling service checkout may sit on another branch
    # than service origin/2.0, whose routes this package is checked against.
    env = os.environ.get("SMARTMEMORY_SERVICE_CHECKOUT")
    if env:
        candidate = Path(env) / SNAPSHOT_REL.split("/", 1)[1]
        return candidate if candidate.is_file() else None
    return None


def _routes_from_snapshot(path: Path) -> list[tuple[str, str]]:
    schema = json.loads(path.read_text())
    return [
        (method.upper(), template)
        for template, ops in schema["paths"].items()
        for method in ops
        if method.lower() in HTTP_METHODS
    ]


def _route_table() -> list[tuple[str, str]]:
    snapshot = _find_snapshot()
    if snapshot is not None:
        return _routes_from_snapshot(snapshot)
    return [tuple(r) for r in json.loads(FIXTURE.read_text())["routes"]]


ROUTES = _route_table()


def _template_regex(method: str, template: str) -> re.Pattern:
    out, pos = "^", 0
    for m in re.finditer(r"\{[^}]+\}", template):
        out += re.escape(template[pos : m.start()])
        out += ".*" if (method, template) in PATH_CONVERTORS else "[^/]+"
        pos = m.end()
    return re.compile(out + re.escape(template[pos:]) + "$")


def service_routes(method: str, raw_path: str) -> list[str]:
    """The templates the service would route this request to.

    uvicorn: `raw_path.partition("?")`, then `scope["path"] = unquote(path)`
    (h11_impl.py / httptools_impl.py). Starlette then takes the first full match; when
    there is none it 307-redirects to the slash-toggled path if that matches
    (`redirect_slashes`). Every match is returned, so a test can require exactly one.
    """
    path = unquote(raw_path.partition("?")[0])
    hits = [t for m, t in ROUTES if m == method and _template_regex(m, t).match(path)]
    if hits:
        return hits
    toggled = path[:-1] if path.endswith("/") else path + "/"
    return [
        f"redirect->{t}"
        for m, t in ROUTES
        if m == method and _template_regex(m, t).match(toggled)
    ]


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, *_args, **_kwargs):
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


# One row per by-id request site: (enclosing function, source file, method, the
# service route template it must reach, call). The AST guard below fails when a site
# in the package is missing from this table.
ROWS = [
    ("get", REMOTE, "GET", "/memory/{item_id}", lambda b, i: b.get(i)),
    (
        "explain",
        REMOTE,
        "GET",
        "/memory/{item_id}/explain",
        lambda b, i: b.explain(i),
    ),
    (
        "update",
        REMOTE,
        "PATCH",
        "/memory/{item_id}",
        lambda b, i: b.update(i, content="x"),
    ),
    ("delete", REMOTE, "DELETE", "/memory/{item_id}", lambda b, i: b.delete(i)),
    (
        "decision_get",
        REMOTE,
        "GET",
        "/memory/decisions/{decision_id}",
        lambda b, i: b.decision_get(i),
    ),
    (
        "decision_supersede",
        REMOTE,
        "POST",
        "/memory/decisions/{decision_id}/supersede",
        lambda b, i: b.decision_supersede(i, "new", "why"),
    ),
    (
        "decision_retract",
        REMOTE,
        "POST",
        "/memory/decisions/{decision_id}/retract",
        lambda b, i: b.decision_retract(i, "why"),
    ),
    (
        "decision_reinforce",
        REMOTE,
        "POST",
        "/memory/decisions/{decision_id}/reinforce",
        lambda b, i: b.decision_reinforce(i, "ev"),
    ),
    (
        "decision_contradict",
        REMOTE,
        "POST",
        "/memory/decisions/{decision_id}/contradict",
        lambda b, i: b.decision_contradict(i, "ev"),
    ),
    (
        "decision_provenance",
        REMOTE,
        "GET",
        "/memory/decisions/{decision_id}/provenance",
        lambda b, i: b.decision_provenance(i),
    ),
    (
        "decision_find_conflicts",
        REMOTE,
        "POST",
        "/memory/decisions/{decision_id}/conflicts",
        lambda b, i: b.decision_find_conflicts(i),
    ),
    (
        "decision_resolve_requirement",
        REMOTE,
        "POST",
        "/memory/decisions/pending/{decision_id}/resolve",
        lambda b, i: b.decision_resolve_requirement(i, "req", "mem"),
    ),
    (
        "decision_try_activate",
        REMOTE,
        "POST",
        "/memory/decisions/pending/{decision_id}/activate",
        lambda b, i: b.decision_try_activate(i),
    ),
    (
        "agent_set_recall_profile",
        AGENT,
        "PUT",
        "/memory/agents/{agent_id}/recall-profile",
        _agent("agent_set_recall_profile", {"decision": 2.0}),
    ),
    (
        "agent_get_recall_profile",
        AGENT,
        "GET",
        "/memory/agents/{agent_id}/recall-profile",
        _agent("agent_get_recall_profile"),
    ),
]


def _split(template: str) -> tuple[str, str]:
    prefix, _, rest = template.partition("{")
    return prefix, rest.partition("}")[2]


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
    monkeypatch.setattr(memory_tools, "get_backend", lambda: backend)
    monkeypatch.setattr(decision_tools, "get_backend", lambda: backend)
    return backend, seen


def outcomes(wire, method, template, call, ids):
    """(id, outcome, service routes) per id; outcome is 'refused' or the raw path."""
    backend, seen = wire
    rows = []
    for item_id in ids:
        seen.clear()
        try:
            call(backend, item_id)
        except ValueError:
            rows.append((item_id, "refused" if not seen else "refused-but-sent", []))
            continue
        except Exception:
            pass  # the fake answer's shape is not under test, the request is
        if len(seen) != 1:
            rows.append((item_id, f"sent {len(seen)} requests", []))
            continue
        request = seen[0]
        raw = request.url.raw_path.decode()
        if request.method != method:
            rows.append((item_id, f"method {request.method}", []))
            continue
        rows.append((item_id, raw, service_routes(method, raw)))
    return rows


@pytest.mark.parametrize(
    "name,_file,method,template,call", ROWS, ids=[r[0] for r in ROWS]
)
def test_every_id_reaches_only_its_own_service_route_or_is_refused(
    wire, name, _file, method, template, call
):
    assert (method, template) in ROUTES, f"{name}: {method} {template} not a route"
    prefix, suffix = _split(template)
    bad = []
    for item_id, outcome, routes in outcomes(
        wire, method, template, call, HOSTILE_IDS + NORMAL_IDS
    ):
        if outcome == "refused":
            if item_id in NORMAL_IDS:
                bad.append((item_id, "normal id refused"))
            continue
        if routes != [template]:
            bad.append((item_id, outcome, routes))
        if item_id in NORMAL_IDS and outcome != f"{prefix}{item_id}{suffix}":
            bad.append((item_id, "normal id changed", outcome))
        if outcome.startswith("/") and "?" in outcome:
            bad.append((item_id, "grew a query string", outcome))
    assert bad == [], f"{name}: {bad}"


def _tool(register, name, *rest, **kw):
    def call(item_id):
        mcp = _FakeMCP()
        register(mcp)
        return mcp.fns[name](item_id, *rest, **kw)

    return call


# One row per TOOL whose id argument reaches a request path (the 2.0 tool surface; the
# hosted allowlist re-registers these same functions): (tool, method, template, call).
TOOL_ROWS = [
    (
        "memory_get",
        "GET",
        "/memory/{item_id}",
        _tool(memory_tools.register_free, "memory_get"),
    ),
    (
        "memory_explain",
        "GET",
        "/memory/{item_id}/explain",
        _tool(memory_tools.register_free, "memory_explain"),
    ),
    (
        "memory_update",
        "PATCH",
        "/memory/{item_id}",
        _tool(memory_tools.register_pro, "memory_update", content="x"),
    ),
    (
        "memory_delete",
        "DELETE",
        "/memory/{item_id}",
        _tool(memory_tools.register_pro, "memory_delete"),
    ),
    (
        "decision_get",
        "GET",
        "/memory/decisions/{decision_id}",
        _tool(decision_tools.register, "decision_get"),
    ),
    (
        "decision_supersede",
        "POST",
        "/memory/decisions/{decision_id}/supersede",
        _tool(decision_tools.register, "decision_supersede", "new", "why"),
    ),
    (
        "decision_retract",
        "POST",
        "/memory/decisions/{decision_id}/retract",
        _tool(decision_tools.register, "decision_retract", "why"),
    ),
    (
        "decision_reinforce",
        "POST",
        "/memory/decisions/{decision_id}/reinforce",
        _tool(decision_tools.register, "decision_reinforce", "ev"),
    ),
    (
        "decision_contradict",
        "POST",
        "/memory/decisions/{decision_id}/contradict",
        _tool(decision_tools.register, "decision_contradict", "ev"),
    ),
    (
        "decision_provenance",
        "GET",
        "/memory/decisions/{decision_id}/provenance",
        _tool(decision_tools.register, "decision_provenance"),
    ),
    (
        "decision_find_conflicts",
        "POST",
        "/memory/decisions/{decision_id}/conflicts",
        _tool(decision_tools.register, "decision_find_conflicts"),
    ),
    (
        "decision_resolve_requirement",
        "POST",
        "/memory/decisions/pending/{decision_id}/resolve",
        _tool(decision_tools.register, "decision_resolve_requirement", "req", "mem"),
    ),
    (
        "decision_try_activate",
        "POST",
        "/memory/decisions/pending/{decision_id}/activate",
        _tool(decision_tools.register, "decision_try_activate"),
    ),
    (
        "agent_set_recall_profile",
        "PUT",
        "/memory/agents/{agent_id}/recall-profile",
        _tool(agent_tools.register, "agent_set_recall_profile", {"decision": 2.0}),
    ),
    (
        "agent_get_recall_profile",
        "GET",
        "/memory/agents/{agent_id}/recall-profile",
        _tool(agent_tools.register, "agent_get_recall_profile"),
    ),
]
TOOL_HOSTILE = ["clear-all", "teams/T1", "../x", "%2e%2e", "a/b", "%2F", ""]


@pytest.mark.parametrize(
    "tool,method,template,call", TOOL_ROWS, ids=[r[0] for r in TOOL_ROWS]
)
def test_every_tool_id_is_refused_or_reaches_only_its_own_route(
    wire, tool, method, template, call
):
    _, seen = wire
    prefix, _ = _split(template)
    assert (method, template) in ROUTES, f"{tool}: {method} {template} not a route"
    bad = []
    for item_id in TOOL_HOSTILE + sorted(RESERVED_SEGMENTS[prefix]):
        seen.clear()
        try:
            call(item_id)
        except ValueError:
            if seen:
                bad.append((item_id, "refused but sent", len(seen)))
            continue
        except Exception:
            pass  # the fake answer's shape is not under test, the request is
        raw = [r.url.raw_path.decode() for r in seen]
        if item_id not in RESERVED_SEGMENTS[prefix] or raw:
            routed = [service_routes(method, r) for r in raw]
            if routed != [[template]]:
                bad.append((item_id, raw, routed))
    assert bad == [], f"{tool}: {bad}"


@pytest.mark.parametrize(
    "name,_file,method,template,call", ROWS, ids=[r[0] for r in ROWS]
)
def test_every_reserved_segment_is_refused_at_every_site(
    wire, name, _file, method, template, call
):
    prefix, _ = _split(template)
    reserved = sorted(RESERVED_SEGMENTS[prefix])
    sent = [
        (seg, outcome)
        for seg, outcome, _ in outcomes(wire, method, template, call, reserved)
        if outcome != "refused"
    ]
    assert sent == [], f"{name}: reserved segments reached the wire: {sent}"


# (id, prefix, expected): "refuse", or the exact encoded segment.
PATH_ID_TABLE = [
    ("a/b", "/memory/", "refuse"),
    ("a\\b", "/memory/agents/", "refuse"),
    ("", "/memory/", "refuse"),
    (".", "/memory/decisions/", "refuse"),
    ("..", "/memory/", "refuse"),
    (None, "/memory/", "refuse"),
    ("a\tb", "/memory/", "refuse"),
    ("a\x7fb", "/memory/", "refuse"),
    ("clear-all", "/memory/", "refuse"),
    ("pending", "/memory/decisions/", "refuse"),
    ("create", "/memory/decisions/pending/", "refuse"),
    ("pending", "/memory/", "pending"),  # reserved only where it is a sibling
    ("clear-all", "/memory/decisions/", "clear-all"),
    ("x?y#z %", "/memory/", "x%3Fy%23z%20%25"),
    ("%2F", "/memory/", "%252F"),
    ("mem_abc123", "/memory/", "mem_abc123"),
    (42, "/memory/", "42"),
]


@pytest.mark.parametrize("value,under,expected", PATH_ID_TABLE)
def test_path_id_refuses_or_encodes(value, under, expected):
    if expected == "refuse":
        with pytest.raises(ValueError):
            path_id(value, under=under)
    else:
        assert path_id(value, under=under) == expected


def test_path_id_unknown_prefix_fails_closed():
    with pytest.raises(LookupError):
        path_id("mem_abc123", under="/memory/somewhere-new/")


def test_memory_delete_clear_all_sends_nothing(wire):
    """review-r1/r2 H2: the tool call itself never reaches DELETE /memory/clear-all."""
    _, seen = wire
    mcp = _FakeMCP()
    memory_tools.register_pro(mcp)
    with pytest.raises(ValueError, match="another route"):
        mcp.fns["memory_delete"]("clear-all")
    assert seen == []


def _static_siblings() -> dict[str, dict[str, set[str]]]:
    """prefix -> method -> static path segments directly under that prefix."""
    out: dict[str, dict[str, set[str]]] = {}
    for prefix in RESERVED_SEGMENTS:
        for method, template in ROUTES:
            rest = template[len(prefix) :] if template.startswith(prefix) else ""
            segment = rest.split("/")[0]
            if segment and "{" not in segment:
                out.setdefault(prefix, {}).setdefault(method, set()).update([segment])
    return out


def test_reserved_lists_cover_every_static_sibling_in_the_service():
    prefixes = {_split(r[3])[0] for r in ROWS}
    assert prefixes <= set(RESERVED_SEGMENTS), prefixes - set(RESERVED_SEGMENTS)
    missing = {
        (prefix, method): sorted(segments - RESERVED_SEGMENTS[prefix])
        for prefix, by_method in _static_siblings().items()
        for method, segments in by_method.items()
        if segments - RESERVED_SEGMENTS[prefix]
    }
    assert missing == {}, (
        f"service routes the package does not reserve: {missing}; add them to "
        "RESERVED_SEGMENTS in smartmemory_mcp/backends/path_guard.py"
    )
    siblings = _static_siblings()
    assert "clear-all" in siblings["/memory/"]["DELETE"]
    assert {"pending", "search"} <= siblings["/memory/decisions/"]["GET"]


def test_route_table_fixture_matches_service_snapshot():
    snapshot = _find_snapshot()
    if snapshot is None:
        pytest.skip(f"service repo not found ({SNAPSHOT_REL}); fixture used as is")
    fixture = [tuple(r) for r in json.loads(FIXTURE.read_text())["routes"]]
    assert fixture == _routes_from_snapshot(snapshot), (
        "tests/fixtures/service_route_table.json is stale: run "
        "python3 tests/test_path_id_encoding.py --write-route-table"
    )
    routes_dir = snapshot.parents[3] / "memory_service" / "api"
    declared = set()
    for source in routes_dir.rglob("*.py"):
        for m in re.finditer(
            r'@router\.(\w+)\(\s*"([^"]*:path\}[^"]*)"', source.read_text()
        ):
            template = re.sub(r":path\}", "}", m.group(2))
            declared.update([(m.group(1).upper(), template)])
    in_table = {(m, t) for m, t in ROUTES}
    prefixed = {
        next((m, t) for m, t in in_table if m == method and t.endswith(tail))
        for method, tail in declared
    }
    assert prefixed == PATH_CONVERTORS


def _path_sites():
    """Every place in the package that builds a request path from a value.

    A path literal is a string starting with `/` and holding no whitespace. Sites are
    f-strings with a placeholder, `.format`, `%` and `+` on such a literal. A site is
    guarded when each placeholder is `path_id(x, under=<the literal text before it>)`.
    """

    def is_path_literal(node):
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/")
            and not any(c.isspace() for c in node.value)
        )

    def guarded(parts):
        text = ""
        for part in parts:
            if isinstance(part, ast.Constant):
                text += part.value
                continue
            call = part.value
            ok = (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "path_id"
                and len(call.args) == 1
                and [k.arg for k in call.keywords] == ["under"]
                and isinstance(call.keywords[0].value, ast.Constant)
                and call.keywords[0].value.value == text
            )
            if not ok:
                return False
            text += "{id}"
        return True

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
                    sites.append((rel, enclosing(node), guarded(parts)))
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
    unguarded = [(f, fn) for f, fn, ok in sites if not ok]
    assert unguarded == [], f"ids in a request path without path_id: {unguarded}"
    found = {(f, fn) for f, fn, _ in sites}
    table = {(r[1], r[0]) for r in ROWS}
    assert found == table, (
        f"in the package but not the table: {sorted(found - table)}; "
        f"in the table but not the package: {sorted(table - found)}"
    )
    assert len(sites) == len(ROWS)


def _write_route_table() -> None:
    snapshot = _find_snapshot()
    if snapshot is None:
        sys.exit(f"service snapshot not found: {SNAPSHOT_REL}")
    lines = [json.dumps(list(r)) for r in _routes_from_snapshot(snapshot)]
    FIXTURE.write_text(
        "{\n"
        f'"_source": "{SNAPSHOT_REL} (paths, in snapshot order)",\n'
        '"_regenerate": "python3 tests/test_path_id_encoding.py --write-route-table",\n'
        '"routes": [\n' + ",\n".join(lines) + "\n]\n}\n"
    )


if __name__ == "__main__" and "--write-route-table" in sys.argv:
    _write_route_table()
