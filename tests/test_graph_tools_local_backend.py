"""`memory_resolve_aliases` MCP tool — local + remote dispatch (CORE-GRAPH-ALIAS-RESOLVE-2 B2).

Mirrors the forcing-function pattern of ``tests/test_decision_tools_local_backend.py`` /
``tests/test_agent_evaluation_get_contract.py``: the standalone MCP venv ships neither
``smartmemory`` nor ``smartmemory_app``, so a real ``LocalBackend`` cannot be constructed
here. Instead we capture the registered tool via a fake MCP and stand in a backend that
exposes ``_mem`` (local) or ``request`` (remote), asserting:

- LOCAL: the tool calls ``backend._mem.resolve_aliases(dry_run=...)`` on the real
  SmartMemory (NOT the MCP wrapper), consumes the ``AliasResolveReport.to_dict()`` shape,
  and never passes ``workspace_id`` (scope derives from auth).
- REMOTE: the tool POSTs ``/memory/graph/resolve-aliases`` with ``dry_run`` as a QUERY
  parameter, and renders the JSON response.
- The human-readable summary reports resolved / abstained / redirected / ambiguous, and a
  dry run is clearly flagged as a no-change preview.

The local report stand-in carries the EXACT ``to_dict()`` shape from
``smartmemory.graph.alias_resolution.AliasResolveReport`` so this test locks the real
contract the tool depends on.
"""

from __future__ import annotations


class _FakeMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _tool():
    from smartmemory_mcp.tools import graph_tools

    mcp = _FakeMCP()
    graph_tools.register(mcp)
    assert "memory_resolve_aliases" in mcp.tools, (
        "memory_resolve_aliases not registered by graph_tools.register()"
    )
    return mcp.tools["memory_resolve_aliases"]


class _Report:
    """Stands in for smartmemory.graph.alias_resolution.AliasResolveReport — same to_dict()."""

    def __init__(self, resolved, abstained, redirected_edges, ambiguous, dry_run, disambiguated=0):
        self._d = {
            "resolved": resolved,
            "abstained": abstained,
            "redirected_edges": redirected_edges,
            "ambiguous": list(ambiguous),
            "disambiguated": disambiguated,
            "dry_run": dry_run,
        }

    def to_dict(self):
        return dict(self._d)


class _SmartMemory:
    """Real-SmartMemory stand-in: only resolve_aliases is exercised."""

    def __init__(self, report):
        self._report = report
        self.calls: list[dict] = []

    def resolve_aliases(self, workspace_id=None, dry_run=False, disambiguate=False):
        # Forcing function: the tool must NOT pass workspace_id (scope from auth). Signature MIRRORS the
        # real SmartMemory.resolve_aliases (CORE-GRAPH-ALIAS-DISAMBIG-1 added the disambiguate kwarg).
        self.calls.append({"workspace_id": workspace_id, "dry_run": dry_run, "disambiguate": disambiguate})
        return self._report


class _LocalBackend:
    """LocalBackend stand-in: exposes _mem (the real SmartMemory), no `request`."""

    def __init__(self, report):
        self._mem = _SmartMemory(report)


class _RemoteBackend:
    """RemoteBackend stand-in: exposes `request`, no `_mem`."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def request(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, "kwargs": kwargs})
        return self._response


# --------------------------------------------------------------------------- local


def test_local_calls_resolve_aliases_on_smart_memory_not_wrapper(monkeypatch):
    """LOCAL: the tool calls backend._mem.resolve_aliases(dry_run=...) — the real
    SmartMemory — and renders its report. Never passes workspace_id."""
    fn = _tool()
    report = _Report(resolved=3, abstained=1, redirected_edges=7, ambiguous=["Hudson"], dry_run=False)
    backend = _LocalBackend(report)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(dry_run=False)

    # Dispatched to the real SmartMemory with the right args, no workspace_id.
    assert backend._mem.calls == [{"workspace_id": None, "dry_run": False, "disambiguate": False}], (
        f"must call _mem.resolve_aliases(dry_run=False, disambiguate=False) with no workspace_id; "
        f"got {backend._mem.calls!r}"
    )
    assert isinstance(out, str)
    # Counts surfaced from AliasResolveReport.to_dict().
    assert "3 alias" in out and "abstained 1" in out and "7 edge" in out, out


def test_local_threads_disambiguate_flag(monkeypatch):
    """LOCAL: the opt-in disambiguate flag reaches the real SmartMemory and the recovered count is surfaced
    (CORE-GRAPH-ALIAS-DISAMBIG-1) — the forcing function that the flag is actually threaded, not dropped."""
    fn = _tool()
    report = _Report(resolved=2, abstained=3, redirected_edges=4, ambiguous=["Hudson"], dry_run=False, disambiguated=1)
    backend = _LocalBackend(report)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(disambiguate=True)

    assert backend._mem.calls == [{"workspace_id": None, "dry_run": False, "disambiguate": True}], backend._mem.calls
    assert "recovered by disambiguation" in out, out
    assert "Hudson" in out, f"abstained surface must be listed: {out!r}"
    assert "preview" not in out.lower() and "dry run" not in out.lower(), (
        f"a real run must not be labelled a preview: {out!r}"
    )


def test_local_dry_run_is_a_preview_with_no_changes(monkeypatch):
    """LOCAL dry run: clearly flagged as a preview, dry_run forwarded to core."""
    fn = _tool()
    report = _Report(resolved=2, abstained=0, redirected_edges=0, ambiguous=[], dry_run=True)
    backend = _LocalBackend(report)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(dry_run=True)

    assert backend._mem.calls == [{"workspace_id": None, "dry_run": True, "disambiguate": False}]
    assert isinstance(out, str)
    low = out.lower()
    assert "preview" in low or "dry run" in low, f"dry run must be flagged: {out!r}"
    assert "no change" in low, f"dry run must say no changes were made: {out!r}"


def test_local_refuses_without_mem(monkeypatch):
    """A backend with neither `request` nor `_mem` -> clear refusal, not a crash."""
    fn = _tool()
    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: object())
    out = fn()
    assert isinstance(out, str)
    assert "local backend" in out.lower() or "remote" in out.lower(), out


# --------------------------------------------------------------------------- remote


def test_remote_posts_with_dry_run_query_param(monkeypatch):
    """REMOTE: POST /memory/graph/resolve-aliases with dry_run as a QUERY parameter."""
    fn = _tool()
    response = {
        "resolved": 5,
        "abstained": 2,
        "redirected_edges": 11,
        "ambiguous": ["Smith", "Jones"],
        "dry_run": False,
        "workspace_id": "ws-1",
        "user_id": "u-1",
    }
    backend = _RemoteBackend(response)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(dry_run=False)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/memory/graph/resolve-aliases"
    # No dry_run query param on a non-dry-run call (service default is False).
    assert call["kwargs"].get("params") in (None, {}), (
        f"non-dry-run call should not force a dry_run param; got {call['kwargs']!r}"
    )
    assert isinstance(out, str)
    assert "5 alias" in out and "abstained 2" in out and "11 edge" in out, out
    assert "Smith" in out and "Jones" in out, out


def test_remote_dry_run_sends_query_param_true(monkeypatch):
    """REMOTE dry run: dry_run=true sent as a query param; rendered as a preview."""
    fn = _tool()
    response = {
        "resolved": 4,
        "abstained": 0,
        "redirected_edges": 0,
        "ambiguous": [],
        "dry_run": True,
    }
    backend = _RemoteBackend(response)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(dry_run=True)

    call = backend.calls[0]
    assert call["kwargs"].get("params") == {"dry_run": "true"}, (
        f"dry run must send ?dry_run=true (lowercase string, like clear_user_memories' nuclear); "
        f"got {call['kwargs']!r}"
    )
    low = out.lower()
    assert "preview" in low or "dry run" in low, out
    assert "no change" in low, out


def test_summary_follows_payload_dry_run_not_request_arg(monkeypatch):
    """[Codex] The summary must trust the report PAYLOAD's `dry_run`, never the
    request arg. If the caller asked for a dry run but the service ignored the
    query param and performed a REAL merge (returning `dry_run=False` with real
    counts), the output must NOT claim "no changes made" and must surface the
    real resolved/redirected counts — otherwise it silently lies about data loss."""
    fn = _tool()
    # Caller requests dry_run=True, but the service actually executed (dry_run=False)
    # and mutated the graph — resolved 6, redirected 9 edges.
    response = {
        "resolved": 6,
        "abstained": 1,
        "redirected_edges": 9,
        "ambiguous": ["Hudson"],
        "dry_run": False,
    }
    backend = _RemoteBackend(response)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(dry_run=True)

    # The request still sent the dry_run query param (caller's intent is honest)...
    assert backend.calls[0]["kwargs"].get("params") == {"dry_run": "true"}
    # ...but the SUMMARY follows the payload: a real execution, no preview claim.
    low = out.lower()
    assert "no change" not in low, (
        f"payload reports a real merge (dry_run=False) — summary must NOT claim "
        f"'no changes made': {out!r}"
    )
    assert "preview" not in low and "dry run" not in low, (
        f"payload reports a real merge — summary must NOT be labelled a preview: {out!r}"
    )
    # Real counts from the payload are surfaced.
    assert "6 alias" in out and "abstained 1" in out and "9 edge" in out, out
    assert "Hudson" in out, out


def test_local_summary_follows_payload_dry_run_not_request_arg(monkeypatch):
    """[Codex] Local mirror: caller asks dry_run=True but the report says it ran
    for real (dry_run=False) — the summary follows the payload, not the arg."""
    fn = _tool()
    report = _Report(resolved=2, abstained=0, redirected_edges=4, ambiguous=[], dry_run=False)
    backend = _LocalBackend(report)

    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn(dry_run=True)

    # The request flag is still forwarded to core (caller intent preserved)...
    assert backend._mem.calls == [{"workspace_id": None, "dry_run": True, "disambiguate": False}]
    # ...but the summary trusts the payload: real run, no "no changes" claim.
    low = out.lower()
    assert "no change" not in low and "preview" not in low and "dry run" not in low, out
    assert "2 alias" in out and "4 edge" in out, out


def test_remote_error_dict_is_surfaced(monkeypatch):
    """REMOTE error dict -> readable error string, not a crash."""
    fn = _tool()
    backend = _RemoteBackend({"error": "API error 500: boom"})
    monkeypatch.setattr("smartmemory_mcp.tools.graph_tools.get_backend", lambda: backend)
    out = fn()
    assert isinstance(out, str)
    assert "boom" in out, out
