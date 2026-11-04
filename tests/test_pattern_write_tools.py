"""SM-PATTERN-CRUD-1 (OWNER D-0089): the adherence pattern catalog is writable over MCP.

Local mode: a real Lite store (``create_lite_memory`` under ``tmp_path``, no worker) behind
``LocalBackend``; each tool called the way the server registers it. Remote mode: ``RemoteBackend``
over in-process HTTP to a small app with the service's ``/memory/patterns`` route shapes, backed by
another real Lite store, so the request bodies are the ones the service gets (the service's own
route rows are ``smart-memory-service`` ``tests/security/test_pattern_tenant_isolation.py``).
Seed fixtures are files under ``tmp_path`` only.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from smartmemory_mcp.backends import pattern_seed_reader
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS, _CapturingRegistrar
from smartmemory_mcp.tools import common, pattern_tools

pytest.importorskip("smartmemory.tools.factory")

from smartmemory.adherence import PatternManager  # noqa: E402
from smartmemory.adherence.seed import link_refusal as core_link_refusal  # noqa: E402
from smartmemory.adherence.seed import read_memory_dir as core_read_memory_dir  # noqa: E402
from smartmemory.tools.factory import create_lite_memory  # noqa: E402

WRITE_TOOLS = ("pattern_create", "pattern_update", "pattern_retire", "pattern_seed")
PROTECTED = ("pattern_id", "status", "superseded_by", "created_at")

INDEX = textwrap.dedent("""
    # Project Memory

    ## Quality Rules

    - [test_no_silent_fallbacks](feedback_no_silent_fallbacks.md) — raise on failure.
    - [test_just_do_it](feedback_just_do_it.md) — execute when asked.

    ## Tooling

    - [test_use_codex](feedback_codex.md) — escalate after two attempts.
""").strip()

FILES = {
    "feedback_no_silent_fallbacks.md": "Never return empty on failure.\n",
    "feedback_just_do_it.md": "Just do the task.\n",
    "feedback_codex.md": "Ask Codex after two failed attempts.\n",
}

SECRET = "test_outside_secret_text"


def _memory_dir(root: Path, extra_index: str = "") -> Path:
    d = root / "test_mem"
    d.mkdir()
    for name, text in FILES.items():
        (d / name).write_text(text)
    (d / "MEMORY.md").write_text(INDEX + "\n" + extra_index)
    return d


def _escaping_dir(root: Path) -> Path:
    """A memory dir whose index also links out three ways: absolute, ``..`` and a symlink."""
    outside = root / "test_outside"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET)
    d = _memory_dir(
        root,
        extra_index=(
            "\n## Escapes\n\n"
            f"- [test_abs]({outside / 'secret.md'}) — absolute.\n"
            "- [test_up](../test_outside/secret.md) — parent.\n"
            "- [test_sym](sym_out.md) — symlink out.\n"
        ),
    )
    (d / "sym_out.md").symlink_to(outside / "secret.md")
    return d


@pytest.fixture
def mem(tmp_path):
    memory = create_lite_memory(str(tmp_path / "test_store"), spawn_worker=False)
    try:
        yield memory
    finally:
        memory.close()


def _tools(monkeypatch, backend) -> dict:
    monkeypatch.setattr(common, "_backend", backend)
    registrar = _CapturingRegistrar(FastMCP("test_pattern_write_tools"))
    pattern_tools.register(registrar)
    return {name: tool.function for name, tool in registrar.captured.items()}


@pytest.fixture
def local(monkeypatch, mem):
    backend = LocalBackend()
    backend._mem = mem
    return _tools(monkeypatch, backend)


# -- Local mode on a real store ---------------------------------------------------------------------


def test_headline_create_list_update_retire_locally(local, mem):
    created = local["pattern_create"](
        title="test_prefer_taskgroup",
        content="Prefer TaskGroup over gather",
        severity="warn",
        scope=["**/*.py"],
    )
    pid = created["pattern_id"]
    assert (created["source"], created["updated_by"]) == ("mcp:local", "mcp:local")
    assert pid in {row["pattern_id"] for row in local["pattern_list"]()}

    updated = local["pattern_update"](pattern_id=pid, severity="error")
    assert (updated["severity"], updated["version"]) == ("error", 2)
    stored = PatternManager(mem).get(pid)
    assert (stored.severity, stored.version, stored.updated_by) == (
        "error",
        2,
        "mcp:local",
    )

    retired = local["pattern_retire"](pattern_id=pid, reason="test_superseded")
    assert retired == {"pattern_id": pid, "status": "retracted"}
    node = mem._graph.get_node(pid)  # kept: the retire is soft and auditable
    assert node.metadata["status"] == "retracted"
    assert "test_superseded" in node.metadata["retraction_reason"]
    assert pid not in {row["pattern_id"] for row in local["pattern_list"]()}
    assert local["pattern_query"](free_text="test_prefer_taskgroup") == []


def test_update_cannot_set_a_protected_field(local, mem, monkeypatch):
    pid = local["pattern_create"](title="test_protected", content="x")["pattern_id"]
    params = set(inspect.signature(local["pattern_update"]).parameters)
    assert not params & (
        set(PROTECTED) - {"pattern_id"}
    )  # the tool has no such argument

    backend = common._backend
    for field in PROTECTED:
        with pytest.raises(ValueError, match="protected"):
            backend.pattern_update(pid, **{field: "test_x"})
    stored = PatternManager(mem).get(pid)
    assert (stored.version, stored.status, stored.pattern_id) == (1, "active", pid)


def test_missing_pattern_is_a_message_not_an_empty_success(local):
    assert "not found" in local["pattern_update"](pattern_id="test_absent", title="t")
    assert "not found" in local["pattern_retire"](pattern_id="test_absent")
    assert "No fields to update" in local["pattern_update"](pattern_id="test_absent")


def test_seed_creates_one_pattern_per_link_then_dedupes(local, mem, tmp_path):
    d = _memory_dir(tmp_path)

    first = local["pattern_seed"](dir=str(d))

    assert len(first["created"]) == 3 and first["skipped"] == []
    by_title = {p.title: p for p in PatternManager(mem).queries.list_all()}
    assert (
        by_title["test_no_silent_fallbacks"].severity == "error"
    )  # "never" in the body
    assert by_title["test_just_do_it"].severity == "warn"
    assert by_title["test_use_codex"].severity == "info"  # Tooling section
    assert (
        by_title["test_no_silent_fallbacks"].source
        == "mcp:local:memory_md:Quality Rules"
    )
    assert by_title["test_use_codex"].source == "mcp:local:memory_md:Tooling"
    assert by_title["test_just_do_it"].rationale == "Just do the task."

    again = local["pattern_seed"](dir=str(d))
    assert again["created"] == []
    assert [s["reason"] for s in again["skipped"]] == ["title_exists"] * 3


def test_seed_never_reads_a_link_that_leaves_the_dir(local, mem, tmp_path, caplog):
    d = _escaping_dir(tmp_path)

    with caplog.at_level(logging.WARNING):
        result = local["pattern_seed"](dir=str(d))

    assert len(result["created"]) == 3
    reasons = {s["title"]: s["reason"] for s in result["skipped"]}
    assert reasons == {
        "test_abs": "absolute_link",
        "test_up": "parent_traversal",
        "test_sym": "outside_dir",
    }
    patterns = PatternManager(mem).queries.list_all()
    assert not {p.title for p in patterns} & set(reasons)
    assert all(SECRET not in (p.rationale or "") + p.content for p in patterns)
    for link in (
        str(tmp_path / "test_outside" / "secret.md"),
        "../test_outside/secret.md",
        "sym_out.md",
    ):
        assert link in caplog.text


def test_seed_without_an_index_says_so(local, tmp_path):
    assert "No MEMORY.md" in local["pattern_seed"](dir=str(tmp_path))


# -- Remote mode: text over /memory/patterns, never a path ------------------------------------------


def _service_shaped_app(store, requests: list) -> FastAPI:
    """The service's /memory/patterns route shapes over a real store; records every body."""
    app = FastAPI()
    pm = PatternManager(store)
    ctx = {"workspace_id": "test_ws", "user_id": "test_user"}

    async def record(request: Request):
        body = await request.json() if request.method in ("POST", "PUT") else None
        requests.append(
            (request.method, request.url.path, dict(request.query_params), body)
        )
        return dict(body) if body is not None else None

    @app.get("/memory/patterns")
    async def list_(request: Request):
        await record(request)
        q = request.query_params
        rows = pm.query(
            scope=q.get("scope"),
            severity=q.get("severity"),
            free_text=q.get("q"),
            limit=int(q["limit"]),
        )
        return {"patterns": [p.to_dict() for p in rows], "count": len(rows), **ctx}

    @app.post("/memory/patterns/seed")
    async def seed(request: Request):
        body = await record(request)
        result = store.seed_patterns(
            body["index_text"],
            body["files"],
            refused=body["refused"],
            source_prefix=body["source_prefix"],
        )
        return {**result, **ctx}

    @app.get("/memory/patterns/{pid}")
    async def get(pid: str, request: Request):
        await record(request)
        p = pm.get(pid)
        if p is None:
            raise HTTPException(404, "not found")
        return {"pattern": p.to_dict(), **ctx}

    @app.post("/memory/patterns")
    async def create(request: Request):
        body = await record(request)
        p = pm.create(**body, updated_by="rest:test_user")
        return {"pattern": p.to_dict(), "pattern_id": p.pattern_id, **ctx}

    @app.put("/memory/patterns/{pid}")
    async def update(pid: str, request: Request):
        body = await record(request)
        if set(body) & set(PROTECTED):
            raise HTTPException(422, "protected field")
        if pm.get(pid) is None:
            raise HTTPException(404, "not found")
        tag = body.pop("updated_by", None)
        p = pm.update(pid, **body, updated_by=f"{tag}@rest:test_user")
        return {"pattern": p.to_dict(), **ctx}

    @app.delete("/memory/patterns/{pid}")
    async def delete(pid: str, request: Request):
        await record(request)
        if pm.get(pid) is None:
            raise HTTPException(404, "not found")
        pm.delete(pid, reason=request.query_params["reason"])
        return {"pattern_id": pid, "status": "retracted", **ctx}

    return app


@pytest.fixture
def remote(monkeypatch, mem):
    requests: list = []
    with TestClient(_service_shaped_app(mem, requests)) as client:
        backend = RemoteBackend(api_url="http://testserver", api_key="test_pattern_key")
        backend._session["_bootstrapped"] = True
        backend._session["access_token"] = "test_pattern_token"

        def forward(request: httpx.Request) -> httpx.Response:
            # Starlette's TestClient raises its own httpx2 errors; answer through a real httpx
            # transport so RemoteBackend sees the httpx.HTTPStatusError the service gives it.
            reply = client.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                reply.status_code,
                content=reply.content,
                headers={"content-type": reply.headers.get("content-type", "")},
            )

        wire = httpx.Client(transport=httpx.MockTransport(forward))
        monkeypatch.setattr(
            "smartmemory_mcp.backends.remote.httpx.request",
            lambda method, url, **kw: wire.request(
                method, url, **{k: v for k, v in kw.items() if k != "timeout"}
            ),
        )
        yield _tools(monkeypatch, backend), requests


def test_remote_create_update_retire_through_the_routes(remote, mem):
    tools, requests = remote

    created = tools["pattern_create"](
        title="test_remote_rule", content="x", severity="info"
    )
    pid = created["pattern_id"]
    assert requests[-1][:2] == ("POST", "/memory/patterns")
    assert requests[-1][3]["source"] == "mcp:remote"
    assert created["updated_by"] == "rest:test_user"
    assert pid in {row["pattern_id"] for row in tools["pattern_list"]()}
    assert tools["pattern_get"](pattern_id=pid)["title"] == "test_remote_rule"

    updated = tools["pattern_update"](pattern_id=pid, severity="error")
    assert requests[-1][:2] == ("PUT", f"/memory/patterns/{pid}")
    assert requests[-1][3] == {"severity": "error", "updated_by": "mcp:remote"}
    assert (updated["version"], updated["updated_by"]) == (
        2,
        "mcp:remote@rest:test_user",
    )

    retired = tools["pattern_retire"](pattern_id=pid, reason="test_gone")
    assert requests[-1][:3] == (
        "DELETE",
        f"/memory/patterns/{pid}",
        {"reason": "test_gone"},
    )
    assert retired == {
        "pattern_id": pid,
        "status": "retracted",
    }  # server identity stripped
    assert mem._graph.get_node(pid).metadata["status"] == "retracted"
    assert pid not in {row["pattern_id"] for row in tools["pattern_list"]()}
    assert tools["pattern_get"](pattern_id=pid) is None
    assert "not found" in tools["pattern_retire"](pattern_id=pid)


def test_remote_protected_field_is_a_loud_error(remote):
    tools, _ = remote
    pid = tools["pattern_create"](title="test_remote_protected", content="x")[
        "pattern_id"
    ]
    with pytest.raises(RuntimeError, match="422"):
        common._backend.pattern_update(pid, status="test_x")


def test_remote_seed_sends_text_never_a_path(remote, mem, tmp_path):
    tools, requests = remote
    d = _escaping_dir(tmp_path)

    result = tools["pattern_seed"](dir=str(d))

    method, path, _, body = requests[-1]
    assert (method, path) == ("POST", "/memory/patterns/seed")
    assert set(body) == {"index_text", "files", "refused", "source_prefix"}
    assert body["files"] == FILES
    assert body["refused"] == {"sym_out.md": "outside_dir"}
    flat = repr(body).replace(
        str(tmp_path / "test_outside" / "secret.md"), ""
    )  # the absolute link as written
    assert str(tmp_path) not in flat and SECRET not in flat
    assert len(result["created"]) == 3
    assert {s["reason"] for s in result["skipped"]} == {
        "absolute_link",
        "parent_traversal",
        "outside_dir",
    }
    assert tools["pattern_seed"](dir=str(d))["created"] == []


# -- The two readers and the import boundary ---------------------------------------------------------


def test_mcp_reader_matches_the_core_reader(tmp_path):
    d = _escaping_dir(tmp_path)
    (d / "test_big.md").write_text("x" * (pattern_seed_reader.SEED_MAX_FILE_CHARS + 1))
    with (d / "MEMORY.md").open("a") as fh:
        fh.write(
            "- [test_big](test_big.md) — too large.\n- [test_missing](test_missing.md) — no file.\n"
        )

    assert pattern_seed_reader.read_memory_dir(d / "MEMORY.md") == core_read_memory_dir(
        d / "MEMORY.md"
    )
    for link in (
        "a.md",
        "/etc/x.md",
        "~/x.md",
        "C:\\x.md",
        "\\\\host\\x.md",
        "../x.md",
        "a/../../x.md",
    ):
        assert pattern_seed_reader.link_refusal(link) == core_link_refusal(link), link


def _core_imports(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "smartmemory"
        ):
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names += [
                a.name for a in node.names if a.name.split(".")[0] == "smartmemory"
            ]
    return names


def test_remote_pattern_path_never_imports_core():
    remote_src = Path(inspect.getfile(RemoteBackend)).read_text()
    cls = next(
        n
        for n in ast.walk(ast.parse(remote_src))
        if isinstance(n, ast.ClassDef) and n.name == "RemoteBackend"
    )
    pattern_methods = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("pattern_")
    ]
    assert {m.name for m in pattern_methods} >= {
        "pattern_query",
        "pattern_get",
        "pattern_list",
        *WRITE_TOOLS,
    }
    assert [_core_imports(m) for m in pattern_methods] == [[]] * len(pattern_methods)
    assert (
        _core_imports(ast.parse(Path(pattern_seed_reader.__file__).read_text())) == []
    )

    tools_tree = ast.parse(Path(pattern_tools.__file__).read_text())
    importing = {
        n.name
        for n in ast.walk(tools_tree)
        if isinstance(n, ast.FunctionDef) and _core_imports(n) and n.name != "register"
    }
    assert importing == {
        "memory_get_violation_patterns"
    }  # the rule-file catalog, a different one


def test_no_pattern_tool_is_on_the_hosted_allowlist():
    assert (
        not {"pattern_query", "pattern_get", "pattern_list", *WRITE_TOOLS}
        & HOSTED_TOOLS
    )
