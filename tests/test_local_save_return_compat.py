"""U4.32 (N-02.activate.outer-readers): LocalBackend reads a core save return as a receipt or a legacy shape.

Legacy returns (id string, held dict) are unchanged and import nothing. A receipt (object or wire dict) reads as
its subject id for ``add`` and in full through ``add_with_receipt``. A rejected or unreadable receipt is a typed
error with one WARNING and never an id. The MCP tool text for both arms is pinned (R15: both shapes parse).
No core producer returns a receipt yet, so a scripted ``_mem.add`` stands in for it.
"""

from __future__ import annotations

import builtins
import logging
import sys
from unittest.mock import MagicMock

import pytest

from smartmemory_mcp.backends import local
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import memory_tools

receipt_mod = pytest.importorskip("smartmemory.save.receipt")
save_return = pytest.importorskip("smartmemory_app.save_return")

LOGGER = "smartmemory_mcp.backends.local"
OURS = (LOGGER, "smartmemory_app.save_return")


def ours(caplog):
    """The save-return records only: core's own origin-policy WARNING on ``add`` is not this unit's."""
    return [r for r in caplog.records if r.name in OURS]


def backend(result):
    be = LocalBackend.__new__(LocalBackend)
    be._mem = MagicMock()
    be._mem.add.return_value = result
    return be


def receipt(**over):
    return receipt_mod.build_local_receipt(
        subject=receipt_mod.ReceiptSubject("item_save", item_id="test_item"),
        run_id="test_run",
        processing_stage="stored",
        execution_state="queued",
        outcome=None,
        terminal=False,
        result_availability="pending",
        revision=1,
        **over,
    )


def skipped_receipt():
    return receipt_mod.build_local_receipt(
        subject=receipt_mod.ReceiptSubject("item_save", item_id="test_item"),
        run_id="test_run",
        processing_stage="stored",
        execution_state="completed",
        outcome="skipped",
        terminal=True,
        result_availability="none",
        revision=1,
    )


def rejection(code="idempotency_conflict"):
    return receipt_mod.decode_receipt(
        {
            "schema_version": "1.1",
            "admission_error": {"code": code, "message": f"test {code}"},
        }
    )


HELD = {
    "status": "held",
    "item_id": None,
    "queued": False,
    "reason": "low_confidence",
    "reasons": ["r"],
}

LEGACY_ROWS = [
    ("id string", "test-id-1"),
    ("held dict", HELD),
    ("queued dict", {"item_id": "test-id-2", "queued": True, "run_id": "r1"}),
    ("none", None),
]


@pytest.mark.parametrize(
    "result", [row[1] for row in LEGACY_ROWS], ids=[row[0] for row in LEGACY_ROWS]
)
def test_legacy_returns_are_unchanged_and_import_nothing(result, monkeypatch, caplog):
    real = builtins.__import__

    def no_wrapper(name, *args, **kwargs):
        if name.startswith("smartmemory_app.save_return"):
            raise AssertionError(f"legacy return imported {name}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_wrapper)
    be = backend(result)
    assert be.add("test content") is result
    kept = be.add_with_receipt("test content")
    assert kept.value is result and kept.receipt is None and kept.summary is None
    assert ours(caplog) == []


RECEIPT_ROWS = [
    ("item received", receipt, "test_item (stored)"),
    (
        "G98 extraction skipped",
        skipped_receipt,
        "test_item (Saved, extraction skipped)",
    ),
]


@pytest.mark.parametrize(
    "make, summary",
    [row[1:] for row in RECEIPT_ROWS],
    ids=[row[0] for row in RECEIPT_ROWS],
)
def test_receipt_reads_as_id_and_keeps_the_receipt(make, summary):
    obj = make()
    for shape in (obj, obj.to_dict()):
        be = backend(shape)
        assert be.add("test content") == "test_item"
        kept = be.add_with_receipt("test content")
        assert (kept.value, kept.receipt, kept.summary) == (
            "test_item",
            obj.to_dict(),
            summary,
        )
        assert be.add("test content") != str(obj)


def test_rejected_receipt_is_typed_logged_and_never_an_id(caplog):
    for shape in (rejection(), rejection().to_dict()):
        caplog.clear()
        be = backend(shape)
        for call in (be.add, be.add_with_receipt):
            with pytest.raises(save_return.SaveRejected):
                call("test content")
        assert [
            r.getMessage() for r in ours(caplog) if r.levelno >= logging.WARNING
        ] == [
            "Save return add rejected at admission (idempotency_conflict); nothing was stored under a new id"
        ] * 2


def test_unreadable_receipt_is_refused(caplog):
    be = backend({**receipt().to_dict(), "schema_version": "2.0"})
    with pytest.raises(save_return.SaveReturnRefused):
        be.add("test content")
    assert [(r.levelno, r.getMessage()) for r in ours(caplog)] == [
        (
            logging.WARNING,
            "Save return add refused: receipt refused by the reader (unsupported_version); no id returned",
        )
    ]


def test_receipt_without_the_wrapper_reader_is_one_warning_and_a_typed_error(
    monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, "smartmemory_app.save_return", None)
    import smartmemory_app

    monkeypatch.delattr(smartmemory_app, "save_return", raising=False)
    be = backend(receipt())
    with pytest.raises(local.SaveReturnUnavailable, match="cannot read it") as raised:
        be.add("test content")
    assert (
        type(raised.value) is local.SaveReturnUnavailable
    )  # the unit's own typed refusal,
    assert isinstance(
        raised.value, RuntimeError
    )  # still a RuntimeError for existing handlers
    assert [(r.levelno, r.name) for r in ours(caplog)] == [(logging.WARNING, LOGGER)]
    assert ours(caplog)[0].getMessage() == (
        "Save return add refused: smartmemory_app.save_return is not installed, so a save receipt "
        "cannot be read; no id returned"
    )


def test_receipt_arm_classifier_table():
    rows = [
        ("test-id", False),
        ({"item_id": "x"}, False),
        (HELD, False),
        (None, False),
        ({"schema_version": "1.1"}, True),
        (receipt(), True),
        (rejection(), True),
    ]
    assert [local._is_receipt_arm(value) for value, _ in rows] == [
        expected for _, expected in rows
    ]


# -- tool text, both arms (R15) -------------------------------------------------------------------------
class FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, **_kwargs):
        def deco(fn):
            self.fns[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def memory_add(monkeypatch):
    def arm(result):
        monkeypatch.setattr(memory_tools, "get_backend", lambda: backend(result))
        mcp = FakeMCP()
        memory_tools.register_pro(mcp)
        return mcp.fns["memory_add"]

    return arm


def test_memory_add_tool_text_is_the_same_for_both_arms(memory_add):
    assert (
        memory_add("test-id-1")("test content")
        == "Memory added (direct). Item ID: test-id-1"
    )
    held = memory_add(HELD)("test content")
    assert held == "Memory held: low_confidence. Details: ['r']"
    for shape in (receipt(), receipt().to_dict()):
        assert (
            memory_add(shape)("test content")
            == "Memory added (direct). Item ID: test_item"
        )


# -- every consumer of the wrapper's ingest in this package, enumerated by AST (review r1 F3) --------------------
def ingest_callers(root, allowed_dynamic=()):
    """Every reference to ``smartmemory_app.storage.ingest`` under ``root``, resolved through the file's imports:
    the bare imported name, an ``as`` alias, ``import M``/``import M as x``, the package attribute chain, a
    relative import, a dynamic import with a constant module name (``importlib.import_module``/``__import__``
    under any alias, as a call receiver), then any dotted chain ending in ``.ingest`` whose root resolves to the
    module.

    Returns ``{(file relative to root, enclosing function, how the return value is used)}``. The scan fails closed:
    a reference it cannot follow (``getattr``, the module or ``ingest`` itself escaping as a value, a star import,
    a string naming the module, a dynamic import whose name is not a constant, the dynamic-import functions or
    ``importlib``/``sys``/``builtins`` escaping as values, ``sys.modules`` used other than by a constant key)
    raises AssertionError naming file:line. ``allowed_dynamic`` is a set of ``(file, function)`` pairs (``<module>``
    for module level) whose module-name strings and non-constant dynamic imports are known not to load the wrapper.
    """
    import ast
    from pathlib import Path

    module, target = "smartmemory_app.storage", "smartmemory_app.storage.ingest"
    tracked = {"smartmemory_app", module, target}
    loaders = {
        "importlib.import_module",
        "importlib.__import__",
        "builtins.__import__",
        "__import__",
    }
    surface = {"importlib", "sys", "builtins"}
    root = Path(root)
    allowed = set(allowed_dynamic)
    found, unresolved = set(), []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        tree = ast.parse(path.read_text())
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        package = [root.name, *rel.parent.parts]
        aliases = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    bound = a.asname or a.name.split(".")[0]
                    aliases.setdefault(bound, set()).add(a.asname and a.name or bound)
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    base = ".".join(
                        [
                            *package[: len(package) - n.level + 1],
                            *filter(None, [n.module]),
                        ]
                    )
                for a in n.names:
                    if a.name == "*":
                        if base in tracked or base in surface:
                            unresolved.append(
                                f"{rel}:{n.lineno}: star import from {base}"
                            )
                        continue
                    aliases.setdefault(a.asname or a.name, set()).add(
                        f"{base}.{a.name}"
                    )

        def loaded(call):
            """What a dynamic import with a constant, absolute module name returns (empty when unreadable)."""
            arg = call.args[0] if call.args else None
            arg = arg or next((k.value for k in call.keywords if k.arg == "name"), None)
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                return set()
            if not arg.value or arg.value.startswith("."):
                return set()
            if resolve(call.func) & {"importlib.import_module"}:
                return {arg.value}
            return {
                arg.value,
                arg.value.split(".")[0],
            }  # __import__ without fromlist returns the top package

        def resolve(node):
            if isinstance(node, ast.Name):
                if node.id == "__import__" and node.id not in aliases:
                    return {"__import__"}
                return aliases.get(node.id, set())
            if isinstance(node, ast.Attribute):
                return {f"{d}.{node.attr}" for d in resolve(node.value)}
            if isinstance(node, ast.Call) and resolve(node.func) & loaders:
                return loaded(node)
            return set()

        def enclosing(node):
            while node in parents and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                node = parents[node]
            return node

        for n in ast.walk(tree):
            p = parents.get(n)
            if (
                isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and (
                    n.value == module
                    or n.value.startswith((module + ".", module + ":"))
                )
                and (str(rel), getattr(enclosing(n), "name", "<module>")) not in allowed
                and not (  # the name of a dynamic import that resolves is enumerated, not a blind reference
                    isinstance(p, ast.Call)
                    and resolve(p.func) & loaders
                    and p.args[:1] == [n]
                )
            ):
                unresolved.append(
                    f"{rel}:{n.lineno}: string {n.value!r} (dynamic lookup)"
                )
            elif (
                isinstance(n, ast.Subscript)
                and ast.unparse(n.value).endswith("modules")
                and isinstance(n.slice, ast.Constant)
                and n.slice.value in tracked
            ):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic lookup)"
                )
            elif (
                isinstance(n, ast.Call)
                and ast.unparse(n.func) in ("getattr", "builtins.getattr")
                and len(n.args) > 1
                and isinstance(n.args[1], ast.Constant)
                and n.args[1].value == "ingest"
            ):
                unresolved.append(
                    f"{rel}:{n.lineno}: getattr(..., 'ingest') on an unresolved receiver"
                )
            elif isinstance(n, ast.Call) and (
                resolve(n.func) & loaders
                or ast.unparse(n.func).split(".")[-1] in ("import_module", "__import__")
            ):
                got = loaded(n)
                first = n.args[0] if n.args else None
                if not resolve(n.func) & loaders:
                    if (
                        isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and first.value in tracked
                    ):
                        unresolved.append(
                            f"{rel}:{n.lineno}: dynamic import of {first.value} on an unresolved receiver"
                        )
                elif not got:
                    if (
                        str(rel),
                        getattr(enclosing(n), "name", "<module>"),
                    ) not in allowed:
                        unresolved.append(
                            f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic import of a name the scan cannot read)"
                        )
                elif got & tracked and not (
                    isinstance(p, ast.Attribute) and p.value is n
                ):
                    unresolved.append(
                        f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic import of {module} or its package used as a value)"
                    )
            if not isinstance(n, (ast.Name, ast.Attribute)) or not isinstance(
                n.ctx, ast.Load
            ):
                continue
            refs = resolve(n)
            if "sys.modules" in refs:
                key = p.slice if isinstance(p, ast.Subscript) and p.value is n else None
                if not (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.split(".")[0] != "smartmemory_app"
                ):
                    unresolved.append(
                        f"{rel}:{n.lineno}: {ast.unparse(n)} (sys.modules used other than by a constant key outside the package)"
                    )
            if refs & surface and not (isinstance(p, ast.Attribute) and p.value is n):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} (import machinery escaping as a value)"
                )
            if refs & loaders and not (isinstance(p, ast.Call) and p.func is n):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} (dynamic import function referenced as a value, not called)"
                )
            if isinstance(p, ast.Attribute) and p.value is n:
                continue  # the chain continues; judged at its end
            hits = refs & tracked
            if not hits:
                continue
            if target not in hits:
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} ({module} or its package) used as a value"
                )
                continue
            if not (isinstance(p, ast.Call) and p.func is n):
                unresolved.append(
                    f"{rel}:{n.lineno}: {ast.unparse(n)} referenced as a value, not called"
                )
                continue
            fn = enclosing(p)
            use_of = parents.get(p)
            use = (
                "discarded"
                if isinstance(use_of, ast.Expr)
                else "returned"
                if isinstance(use_of, ast.Return)
                else "assigned to " + ast.unparse(use_of.targets[0])
                if isinstance(use_of, ast.Assign)
                else "argument of " + ast.unparse(use_of.func)
                if isinstance(use_of, ast.Call)
                else "used in " + type(use_of).__name__
            )
            found.add((str(rel), getattr(fn, "name", "<module>"), use))
    assert not unresolved, (
        "storage.ingest references the census cannot resolve: " + "; ".join(unresolved)
    )
    return found


def test_every_mcp_ingest_consumer_is_assessed():
    """One row: ``LocalBackend.ingest`` returns the wrapper's value (an id string from ``storage.ingest`` with
    ``sync`` left True) and the ``memory_ingest`` tool reads a dict's ``item_id`` or ``str(result)``."""
    from pathlib import Path

    root = Path(local.__file__).parent.parent
    assert ingest_callers(root) == {("backends/local.py", "ingest", "returned")}


# -- the census helper itself, over every import form (review r2 F3): in-memory source, no repository file written ----
CALLER = {("test_scan.py", "caller", "returned")}
CENSUS_ROWS = [
    # (name, source of one file, package name of the scan root, consumers found or the file:line it must fail on)
    (
        "bare imported name",
        "from smartmemory_app.storage import ingest\n\ndef caller():\n    return ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "imported name alias",
        "from smartmemory_app.storage import ingest as save\n\ndef caller():\n    return save('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import module, fully dotted call",
        "import smartmemory_app.storage\n\ndef caller():\n    return smartmemory_app.storage.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import module as alias",
        "import smartmemory_app.storage as st\n\ndef caller():\n    return st.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import package, attribute chain",
        "import smartmemory_app\n\ndef caller():\n    return smartmemory_app.storage.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "from package import module",
        "from smartmemory_app import storage\n\ndef caller():\n    return storage.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "from package import module as alias",
        "from smartmemory_app import storage as s\n\ndef caller():\n    return s.ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "relative import of the name",
        "from .storage import ingest\n\ndef caller():\n    return ingest('test')\n",
        "smartmemory_app",
        CALLER,
    ),
    (
        "relative import of the module",
        "from . import storage\n\ndef caller():\n    x = storage.ingest('test')\n",
        "smartmemory_app",
        {("test_scan.py", "caller", "assigned to x")},
    ),
    (
        "getattr with a string on the module",
        "from smartmemory_app import storage\n\ndef caller():\n    return getattr(storage, 'ingest')('test')\n",
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "getattr with a string on an unknown receiver",
        "def caller(mod):\n    return getattr(mod, 'ingest')('test')\n",
        "test_scan_pkg",
        "test_scan.py:2: ",
    ),
    (
        "module alias made by assignment",
        "from smartmemory_app import storage\n\ndef caller():\n    st = storage\n    return st.ingest('test')\n",
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "dynamic import of the module, constant name (resolved since review r3 F3)",
        "import importlib\n\ndef caller():\n    return importlib.import_module('smartmemory_app.storage').ingest('test')\n",
        "test_scan_pkg",
        CALLER,
    ),
    (
        "ingest passed on as a value",
        "from smartmemory_app import storage\n\ndef caller(run):\n    return run(storage.ingest)\n",
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "star import of the module",
        "from smartmemory_app.storage import *\n\ndef caller():\n    return ingest('test')\n",
        "test_scan_pkg",
        "test_scan.py:1: ",
    ),
    (
        "aliased import_module, call receiver (review r3 F3)",
        'from importlib import import_module as load\n\ndef caller():\n    return load("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "aliased __import__ through builtins, fromlist",
        'from builtins import __import__ as imp\n\ndef caller():\n    return imp("smartmemory_app.storage", fromlist=["ingest"]).ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "import importlib as il, il.import_module",
        'import importlib as il\n\ndef caller():\n    return il.import_module("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "call receiver chain through the package",
        'import importlib\n\ndef caller():\n    return importlib.import_module("smartmemory_app").storage.ingest("test")\n',
        "test_scan_pkg",
        CALLER,
    ),
    (
        "sys.modules aliased by from-import",
        'from sys import modules as registry\n\ndef caller():\n    return registry["smartmemory_app.storage"].ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "sys.modules held in a variable through import sys as s",
        'import sys as s\n\ndef caller():\n    table = s.modules\n    return table["smartmemory_app.storage"].ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "sys.modules read without a constant key",
        'import sys\n\ndef caller():\n    return sys.modules.get("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "module name as a string only",
        'NAME = "smartmemory_app.storage"\n',
        "test_scan_pkg",
        "test_scan.py:1: ",
    ),
    (
        "dynamic import with a name read at run time",
        'import importlib\n\ndef caller(name):\n    return importlib.import_module(name).ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "dynamic import with a relative name",
        'import importlib\n\ndef caller():\n    return importlib.import_module(".storage", "smartmemory_app").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "aliased dynamic import result held in a variable",
        'from importlib import import_module as load\n\ndef caller():\n    m = load("smartmemory_app.storage")\n    return m.ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "importlib itself held in a variable",
        'import importlib\n\ndef caller():\n    loader = importlib\n    return loader.import_module("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "dynamic import function held as a value",
        'from importlib import import_module\n\ndef caller():\n    load = import_module\n    return load("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
    (
        "star import of importlib",
        'from importlib import *\n\ndef caller():\n    return import_module("smartmemory_app.storage").ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:1: ",
    ),
    (
        "getattr on sys passes the registry on",
        'import sys\n\ndef caller():\n    return getattr(sys, "modules")["smartmemory_app.storage"].ingest("test")\n',
        "test_scan_pkg",
        "test_scan.py:4: ",
    ),
]


@pytest.mark.parametrize(
    "source, package, expected",
    [row[1:] for row in CENSUS_ROWS],
    ids=[row[0] for row in CENSUS_ROWS],
)
def test_census_resolves_every_import_form_or_fails_naming_the_line(
    monkeypatch, source, package, expected
):
    import re
    from pathlib import Path

    root = Path(package)
    with monkeypatch.context() as mp:
        mp.setattr(Path, "rglob", lambda self, pattern: [root / "test_scan.py"])
        mp.setattr(Path, "read_text", lambda self, *a, **k: source)
        if isinstance(expected, str):
            with pytest.raises(AssertionError, match=re.escape(expected)):
                ingest_callers(root)
            return
        found = ingest_callers(root)
    assert found == expected


def test_census_allows_a_named_dynamic_import_and_no_other(monkeypatch):
    import re
    from pathlib import Path

    source = "import importlib\n\ndef loader(name):\n    return importlib.import_module(name)\n"
    root = Path("test_scan_pkg")
    with monkeypatch.context() as mp:
        mp.setattr(Path, "rglob", lambda self, pattern: [root / "test_scan.py"])
        mp.setattr(Path, "read_text", lambda self, *a, **k: source)
        assert ingest_callers(root, {("test_scan.py", "loader")}) == set()
        for allowed in ((), {("test_scan.py", "other")}, {("other.py", "loader")}):
            with pytest.raises(AssertionError, match=re.escape("test_scan.py:4: ")):
                ingest_callers(root, allowed)


#: sha256 of ``ingest_callers`` source. The wrapper and the MCP package each hold a copy (the repositories cannot
#: import each other), and both tests pin this one hash, so the copies cannot drift apart unnoticed.
INGEST_CALLERS_SHA256 = (
    "5b79b81b0da964c53a498925b7f3ae396d00c8d1477ca687628e1a0d24d8888e"
)


def test_census_helper_copy_matches_its_twin():
    import hashlib
    import inspect

    digest = hashlib.sha256(inspect.getsource(ingest_callers).encode()).hexdigest()
    assert digest == INGEST_CALLERS_SHA256, (
        "ingest_callers changed: update the copy in the other repository and this hash together"
    )


INGEST_ARMS = [
    # (name, core return, id the tool prints)
    ("legacy id", "test-id", "test-id"),
    ("legacy queued dict", {"item_id": "test-id", "queued": False}, "test-id"),
    ("receipt", receipt(), "test_item"),
    ("receipt wire", receipt().to_dict(), "test_item"),
    ("terminal skipped receipt", skipped_receipt(), "test_item"),
]


@pytest.mark.parametrize(
    "result, expected",
    [row[1:] for row in INGEST_ARMS],
    ids=[row[0] for row in INGEST_ARMS],
)
def test_memory_ingest_tool_reads_the_id_on_both_arms(
    monkeypatch, tmp_path, result, expected
):
    from smartmemory_app import storage

    class ScriptedMemory:
        def ingest(self, content, context=None, sync=None):
            return result

    monkeypatch.setattr(storage, "_data_path", tmp_path)
    monkeypatch.setattr(storage, "get_memory", lambda: ScriptedMemory())
    monkeypatch.setattr(storage, "llm_extraction_warning", lambda: None)
    monkeypatch.setattr(
        memory_tools, "get_backend", lambda: LocalBackend.__new__(LocalBackend)
    )
    mcp = FakeMCP()
    memory_tools.register_free(mcp)
    assert (
        mcp.fns["memory_ingest"]("test content")
        == f"Memory ingested (pipeline). Item ID: {expected}"
    )
