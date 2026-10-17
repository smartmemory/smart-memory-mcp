#!/usr/bin/env python3
"""Save-then-read census of this repo's test suites (C-49.contract.mcp-suites, U49.A2; G11, G12, G47).

Derives the save vocabulary from the product code (never hand-listed), scans every test for the units that save
(directly, by fixture or by helper) and whether a read follows, and checks that every unit carries exactly one
authored disposition. A deferral names where and its owner step and carries the prepared explicit wait
(``tests/public_wait.py``). The ENGINE and COMMON regions are byte-identical in smart-memory-client and
smart-memory-mcp, which share no package.

    python scripts/save_then_read_census.py            # check, exit 1 on a problem or a stale fixture
    python scripts/save_then_read_census.py --write    # regenerate tests/data/save_then_read_census.json
    python scripts/save_then_read_census.py --table    # one line per unit

Stdlib only; it imports none of the code it scans.
"""

import ast
import hashlib
import json
import re
import sys
from pathlib import Path


# --- ENGINE BEGIN ---

SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "dist",
    "build",
    "artifacts",
}
MEMORY_RECEIVER = re.compile(
    r"(?i)(backend|^be$|mem|^sm$|smart|lite|storage|core|client|secure|wrapper|^st$|svc|service)"
)
READ_NAME = re.compile(
    r"^(get|search|recall|list|query|explain|find|read|traverse|stats)(_\w+)?$"
)
WAIT_NAME = re.compile(r"\b(sleep|poll|wait_for|await_\w+|wait)\s*\(")


def _name(node):
    """Dotted name of a Name/Attribute chain, or the unparsed text for anything else (a call, a subscript)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ast.unparse(node)[:40]


def _fixture_kind(fn):
    """None for a plain function, "fixture" for ``@pytest.fixture``, "autouse" for ``@pytest.fixture(autouse=True)``."""
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        if _name(target).split(".")[-1] == "fixture":
            if isinstance(d, ast.Call):
                for kw in d.keywords:
                    if (
                        kw.arg == "autouse"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        return "autouse"
            return "fixture"
    return None


def _walk_functions(tree):
    """Yield (qualified_name, node): ``Class.method`` for methods, the bare name otherwise."""

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{prefix}{child.name}", child
                yield from visit(child, f"{prefix}{child.name}.<locals>.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def _own_nodes(fn):
    """Nodes of ``fn`` excluding nested function bodies (those are their own units)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _memory_like(receiver):
    return bool(MEMORY_RECEIVER.search(receiver.split(".")[-1]))


def call_sites(fn, vocab):
    """Every save and read call in ``fn``: sorted (line, kind, name, receiver, memory_like)."""
    out = set()
    for node in _own_nodes(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr, recv = node.func.attr, _name(node.func.value)
            if attr in vocab["save_methods"]:
                out.add((node.lineno, "save", attr, recv, _memory_like(recv)))
            elif READ_NAME.match(attr):
                out.add((node.lineno, "read", attr, recv, _memory_like(recv)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in vocab["save_tools"]:
                out.add((node.lineno, "save", node.value, "<tool-name>", True))
            elif node.value in vocab["read_tools"]:
                out.add((node.lineno, "read", node.value, "<tool-name>", True))
    return sorted(out)


def _test_files(repo, tests_dir):
    for p in sorted((Path(repo) / tests_dir).rglob("*.py")):
        if not (set(p.relative_to(repo).parts) & SKIP_DIRS):
            yield p


def scan(repo, vocab, *, tests_dir="tests", ignore=(), read_source=None):
    """The census of ``repo``: every save site, and every unit that saves directly or by fixture/helper.

    A unit is a function with a memory-like save site, or one that consumes a fixture or helper that has one
    (a fixture by parameter name, an autouse fixture by sharing its class, a helper by calling it).
    ``reads_after_save`` is True when any read call follows the save point of the unit (conservative: the read
    receiver need not look like memory, so a read through a store handle still flags it). ``ignore`` lists
    repo-relative files skipped by name. ``read_source(path) -> str`` lets a test feed synthetic text.
    """
    repo = Path(repo)
    read_source = read_source or (lambda p: Path(p).read_text())
    sites, funcs, parse_errors = [], {}, []
    for path in _test_files(repo, tests_dir):
        rel = path.relative_to(repo).as_posix()
        if rel in ignore:
            continue
        try:
            tree = ast.parse(read_source(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            parse_errors.append({"file": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for qual, fn in _walk_functions(tree):
            calls = call_sites(fn, vocab)
            call_lines = {}
            for n in _own_nodes(fn):
                if isinstance(n, ast.Call):
                    call_lines[_name(n.func).split(".")[-1]] = min(
                        n.lineno, call_lines.get(_name(n.func).split(".")[-1], n.lineno)
                    )
            funcs[(rel, qual)] = {
                "line": fn.lineno,
                "bare": fn.name,
                "fixture": _fixture_kind(fn),
                "params": [a.arg for a in fn.args.args + fn.args.kwonlyargs],
                "calls": calls,
                "call_lines": call_lines,
            }
            for line, kind, name, recv, mem in calls:
                if kind == "save":
                    sites.append(
                        {
                            "file": rel,
                            "line": line,
                            "function": qual,
                            "call": name,
                            "receiver": recv,
                            "memory_like": mem,
                        }
                    )
    units = {}

    def add_unit(rel, qual, info, via, save_line, saves):
        key = f"{rel}::{qual}"
        row = units.setdefault(
            key,
            {
                "key": key,
                "file": rel,
                "line": info["line"],
                "function": qual,
                "via": set(),
                "saves": set(),
                "reads": set(),
                "save_point": save_line,
            },
        )
        row["via"].add(via)
        row["saves"] |= {(s[0], s[2]) for s in saves}
        row["save_point"] = min(row["save_point"], save_line)
        row["reads"] |= {(r[0], r[2]) for r in info["calls"] if r[1] == "read"}

    for (rel, qual), info in sorted(funcs.items()):
        saves = [c for c in info["calls"] if c[1] == "save" and c[4]]
        if not saves:
            continue
        add_unit(rel, qual, info, "direct", min(s[0] for s in saves), saves)
        if not (info["fixture"] or not info["bare"].startswith("test")):
            continue
        cls = qual.rsplit(".", 1)[0] if "." in qual else None
        for (crel, cqual), cinfo in sorted(funcs.items()):
            if (crel, cqual) == (rel, qual):
                continue
            ccls = cqual.rsplit(".", 1)[0] if "." in cqual else None
            if info["fixture"] == "autouse":
                hit, point = (
                    crel == rel and ccls == cls and cinfo["bare"].startswith("test"),
                    cinfo["line"],
                )
            elif info["fixture"]:
                hit, point = (
                    crel.rsplit("/", 1)[0] == rel.rsplit("/", 1)[0]
                    and info["bare"] in cinfo["params"],
                    cinfo["line"],
                )
            else:
                hit, point = (
                    crel == rel and info["bare"] in cinfo["call_lines"],
                    cinfo["call_lines"].get(info["bare"], cinfo["line"]),
                )
            if hit:
                kind = {"autouse": "autouse fixture", "fixture": "fixture"}.get(
                    info["fixture"], "helper"
                )
                add_unit(crel, cqual, cinfo, f"{kind} {rel}::{qual}", point, [])
    rows = []
    for row in units.values():
        row["reads_after_save"] = any(r[0] >= row["save_point"] for r in row["reads"])
        row["via"] = sorted(row["via"])
        row["saves"] = [list(x) for x in sorted(row["saves"])]
        row["reads"] = [list(x) for x in sorted(row["reads"])]
        del row["save_point"]
        rows.append(row)
    return {
        "sites": sorted(sites, key=lambda s: (s["file"], s["line"], s["call"])),
        "rows": sorted(rows, key=lambda r: r["key"]),
        "parse_errors": parse_errors,
    }


def blind_spots(repo, *, tests_dir="tests", ignore=()):
    """What the AST census cannot read as save-then-read: sleep/poll/wait callers and non-Python suite files."""
    repo, out = Path(repo), {"waits": [], "non_python": []}
    for path in sorted((repo / tests_dir).rglob("*")):
        if not path.is_file() or set(path.relative_to(repo).parts) & SKIP_DIRS:
            continue
        rel = path.relative_to(repo).as_posix()
        if rel in ignore:
            continue
        if path.suffix == ".py":
            text = path.read_text(
                errors="replace"
            )  # an undecodable file is reported by scan(), not raised here
            out["waits"] += [
                f"{rel}:{i}"
                for i, line in enumerate(text.splitlines(), 1)
                if WAIT_NAME.search(line)
            ]
        elif path.suffix in (
            ".js",
            ".mjs",
            ".ts",
            ".tsx",
            ".sh",
            ".yaml",
            ".yml",
            ".json",
            ".feature",
        ):
            out["non_python"].append(rel)
    return out


def import_edges(repo, module_prefix, *, tests_dir="tests", ignore=()):
    """Test files that import ``module_prefix`` (the core wait helper must never appear here): sorted ``file:line``."""
    hits = []
    for path in _test_files(repo, tests_dir):
        rel = path.relative_to(repo).as_posix()
        if rel in ignore:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue  # scan() records this file in parse_errors, which fails the census by name
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(
                n == module_prefix or n.startswith(module_prefix + ".") for n in names
            ):
                hits.append(f"{rel}:{node.lineno}")
    return sorted(hits)


# --- ENGINE END ---


REPO = "smart-memory-mcp"
IGNORE = (
    "tests/public_wait.py",
    "tests/test_public_wait.py",
    "tests/test_save_then_read_census.py",
)
SAVE_METHOD = re.compile(
    r"^(add|add_with_receipt|ingest(_\w+)?|import_\w+|save|save_call|submit_save)$"
)
NON_MEMORY = [
    "tests/test_code_index_repo_key.py:67",
    "tests/test_hosted_tools_allowlist.py:300",
    "tests/test_local_save_return_compat.py:283",
    "tests/test_local_save_return_compat.py:300",
    "tests/test_local_save_return_compat.py:455",
    "tests/test_portability.py:55",
]
WAIT_USERS = {
    "tests/test_hosted_identity.py": "an asyncio Event barrier between concurrent tool calls, no save",
    "tests/test_local_backend_lazy.py": "a thread Event handshake around lazy storage init, no save",
    "tests/test_windows_credential_store.py": "a lock-contention sleep around key storage, no memory save",
}
# U4.42 (worktree wt-mcp-U4.42, head ce73ac5, merge-base 915bbdb) adds tests/test_local_save_call.py. Until it lands
# these 13 units are its rows; the check fails the moment the file appears, so each gets a disposition then.
OWNED_BY_U4_42 = {
    "file": "tests/test_local_save_call.py",
    "head": "ce73ac5",
    "units": [
        [
            "test_save_mints_a_key_in_cores_alphabet_and_names_the_mcp_local_producer",
            114,
        ],
        [
            "test_a_supplied_key_is_carried_and_a_resubmitted_call_sends_the_same_request",
            128,
        ],
        ["test_two_calls_without_a_key_mint_two_keys", 147],
        ["test_save_passes_each_field_to_core_unjudged", 165],
        ["test_data_saves_do_not_extract_by_default", 176],
        [
            "test_an_unreachable_wait_is_cores_typed_refusal_and_the_manager_is_untouched",
            186,
        ],
        ["test_the_facade_off_is_cores_refusal_and_never_add_or_ingest", 197],
        ["test_a_rejected_admission_is_save_rejected_with_one_warning", 216],
        ["test_ingest_structured_legacy_returns_are_unchanged_and_import_nothing", 340],
        [
            "test_a_receipt_the_wrapper_cannot_read_is_one_marked_refusal_and_never_an_id",
            362,
        ],
        ["test_ingest_structured_reads_a_receipt_as_its_subject_id_and_keeps_it", 379],
        ["test_ingest_structured_never_turns_a_rejection_into_an_id", 389],
        ["test_the_structured_tool_text_is_the_same_for_both_arms", 433],
    ],
}
WORKER_OWNER = "the later-ring real-worker parity suite (G47; graph C-49.contract.mcp-suites names no step id for it, filed as a follow-up)"


def _backend_classes(repo):
    for name in ("interface", "local", "remote"):
        path = Path(repo) / "smartmemory_mcp" / "backends" / f"{name}.py"
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                yield node


def _is_tool(fn):
    return any(
        ".tool" in ast.unparse(d) or ast.unparse(d) == "tool" for d in fn.decorator_list
    )


def derive_vocab(repo):
    """Save methods from the backend classes, save and read tools from the registered tool bodies (never hand-listed)."""
    repo = Path(repo)
    methods = {
        m.name
        for cls in _backend_classes(repo)
        for m in cls.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        and SAVE_METHOD.match(m.name)
    }
    save_tools, read_tools = set(), set()
    for path in sorted((repo / "smartmemory_mcp" / "tools").glob("*.py")):
        for top in ast.parse(path.read_text()).body:
            if not (
                isinstance(top, ast.FunctionDef) and top.name.startswith("register")
            ):
                continue
            for tool in ast.walk(top):
                if not (
                    isinstance(tool, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and tool is not top
                    and _is_tool(tool)
                ):
                    continue
                for call in ast.walk(tool):
                    if isinstance(call, ast.Call) and isinstance(
                        call.func, ast.Attribute
                    ):
                        if call.func.attr in methods:
                            save_tools.add(tool.name)
                        elif READ_NAME.match(call.func.attr) and "backend" in _name(
                            call.func.value
                        ):
                            read_tools.add(tool.name)
    return {
        "save_methods": sorted(methods),
        "save_tools": sorted(save_tools),
        "read_tools": sorted(read_tools - save_tools),
    }


def D(cls, why, where=None, prepared=None):
    out = {"class": cls, "why": why}
    if where:
        out |= {"where": where, "owner": WORKER_OWNER, "prepared": prepared}
    return out


HR = "tests/integration/test_hold_receipt.py"
WAIT = 'await_public(observe_get(backend, item_id), wait="terminal", deadline_s=30.0)'
MOCKED = "the backend, core or transport is stubbed or a fake: no worker exists, so there is nothing to wait for"
LOCAL = "a real Lite core (spawn_worker=False or lite_context) runs the save inline (G47), so the read sees it with no wait and no pin"
DISPOSITIONS = {
    f"{HR}::held": D(
        "explicit_sync_kept",
        "ingest(sync=True) per call at the helper: an explicit pin the C1 guard allows (G47), kept as is",
    ),
    f"{HR}::test_tools_report_real_hold_without_success": D(
        "deferred_real_worker",
        "the seven save tools report a real hold only under the explicit sync=True at held(); the worker-path twin (queued then held) has no test, and the empty-store read at :121 cannot tell held from queued",
        [f"{HR}:52", f"{HR}:121"],
        WAIT,
    ),
    f"{HR}::test_remote_add_returns_real_hold": D(
        "deferred_real_worker",
        "RemoteBackend.add is fed a real core hold under the explicit sync=True at held(); the worker-path hold is untested",
        [f"{HR}:52", f"{HR}:128"],
        WAIT,
    ),
    f"{HR}::test_conversation_formatter_preserves_real_chunk_hold": D(
        "deferred_real_worker",
        "real core ingest_conversation chunk hold, formatted by the tool; no worker ran, so a queued chunk hold is unproven",
        [f"{HR}:145"],
        WAIT,
    ),
    "tests/test_edge_tool_output.py::test_edge_tool_exposes_persisted_evidence": D(
        "local_inline",
        LOCAL + "; the hosted arm is an in-process ASGI route over the same store",
    ),
    "tests/test_edge_tool_output.py::test_legacy_code_warns_about_missing_source_evidence": D(
        "local_inline", LOCAL
    ),
    "tests/test_code_index_ingest_fixes.py::_tool": D(
        "not_a_save",
        "returns the registered code_index tool function; saves nothing itself",
    ),
    "tests/test_code_index_ingest_fixes.py::test_default_code_index_skips_a_failing_js_file_instead_of_refusing": D(
        "local_inline", LOCAL
    ),
    "tests/test_code_index_ingest_fixes.py::test_local_partial_parse_retains_prior_sqlite_generation": D(
        "local_inline", LOCAL
    ),
    "tests/test_code_index_ingest_fixes.py::test_remote_parser_dependency_arms": D(
        "mocked_seam",
        "the hosted arm is an in-process ASGI route that publishes inline into SQLite: no worker",
    ),
    "tests/test_local_backend_lazy.py::test_pro_tools_list_does_not_call_storage_get_memory": D(
        "not_a_save",
        "tools/list only; storage is a stub that fails if touched, and no tool is called",
    ),
    "tests/test_tier_registration.py::TestToolRegistration.test_pro_plus_tier_tool_count": D(
        "not_a_save", "counts registered tool names; saves nothing"
    ),
    "tests/test_tier_registration.py::TestToolRegistration.test_pro_tier_tool_count": D(
        "not_a_save", "counts registered tool names; saves nothing"
    ),
}
for _key in (
    "test_backend_conformance.py::test_advertised_document_tool_executes",
    "test_backend_conformance.py::test_advertised_document_tool_executes.<locals>.check",
    "test_backend_conformance.py::test_capabilities_filter_listings_and_reject_unsupported_calls",
    "test_dist_lite_quiet_local_origin.py::test_add_respects_caller_supplied_origin",
    "test_dist_lite_quiet_local_origin.py::test_add_sets_mcp_memory_add_origin",
    "test_dist_lite_quiet_local_origin.py::test_ingest_passes_mcp_memory_ingest_origin",
    "test_dist_lite_quiet_local_origin.py::test_ingest_respects_caller_supplied_origin",
    "test_ingest_document.py::test_local_ingest_document",
    "test_ingest_document.py::test_local_ingest_document_propagates_loader_error",
    "test_ingest_document.py::test_remote_ingest_document",
    "test_ingest_document.py::test_remote_ingest_document_surfaces_service_error",
    "test_local_save_return_compat.py::memory_add.<locals>.arm",
    "test_local_save_return_compat.py::test_legacy_returns_are_unchanged_and_import_nothing",
    "test_local_save_return_compat.py::test_memory_ingest_tool_reads_the_id_on_both_arms",
    "test_local_save_return_compat.py::test_receipt_reads_as_id_and_keeps_the_receipt",
    "test_local_save_return_compat.py::test_receipt_without_the_wrapper_reader_is_one_warning_and_a_typed_error",
    "test_local_save_return_compat.py::test_unreadable_receipt_is_refused",
    "test_portability.py::test_local_backend_import_constructs_direct_corpus_importer",
    "test_portability.py::test_remote_import_passes_active_workspace_to_headers_and_request",
    "test_origin_context.py::test_local_context",
    "test_origin_context.py::test_remote_context",
    "test_remote_code_ingest.py::test_remote_ingest_code_preserves_upload_payload",
    "test_remote_error_surfacing.py::test_add_raises_instead_of_returning_the_error_dict_as_an_item_id",
    "test_remote_error_surfacing.py::test_add_raises_when_no_item_id_comes_back",
    "test_remote_error_surfacing.py::test_add_returns_id_from_the_id_key",
):
    DISPOSITIONS["tests/" + _key] = D("mocked_seam", MOCKED)

# --- COMMON BEGIN (byte-identical in smart-memory-client and smart-memory-mcp scripts/save_then_read_census.py) ---

FIXTURE = "tests/data/save_then_read_census.json"
CLASSES = {
    "mocked_seam": "the transport or core is stubbed; no worker exists, so there is nothing to wait for",
    "local_inline": "a real local Lite core runs the save inline (G47); the read sees it, no wait, and no sync pin is added",
    "explicit_sync_kept": "a per-call sync=True the C1 guard classes EXPLICIT (G47 allows it); not a hidden pin",
    "save_only": "saves and never reads back, so it cannot observe a pending save",
    "not_a_save": "the name matches the vocabulary but nothing is saved (a tool-name list, a count, a registration)",
    "converted": "now waits with await_public (wait_line names the call)",
    "deferred_ring3_sdk_harness": "real-service SDK save-then-read, owned by the ring-3 real SDK harness (graph: C-49.contract.sdk-suites)",
    "deferred_real_worker": "real core or worker save-then-read candidate, carried into the later-ring real-worker harness (graph: C-49.contract.mcp-suites)",
}
DEFERRED = ("deferred_ring3_sdk_harness", "deferred_real_worker")


def engine_sha(script_text):
    """sha256 of the ENGINE region, so an edit to the engine without regenerating the fixture is caught."""
    start = script_text.index("# --- ENGINE BEGIN ---")
    end = script_text.index("# --- ENGINE END ---") + len("# --- ENGINE END ---")
    return hashlib.sha256(script_text[start:end].encode()).hexdigest()[:16]


def build(repo, *, script_text=None):
    """The census document for ``repo``: derived vocabulary, every unit with its disposition, every save site."""
    repo = Path(repo)
    script_text = script_text if script_text is not None else Path(__file__).read_text()
    vocab = derive_vocab(repo)
    engine_vocab = {
        "save_methods": set(vocab["save_methods"]),
        "save_tools": set(vocab["save_tools"]),
        "read_tools": set(vocab["read_tools"]),
    }
    found = scan(repo, engine_vocab, ignore=IGNORE)
    spots = blind_spots(repo, ignore=(FIXTURE,) + IGNORE)
    units = []
    for row in found["rows"]:
        units.append({**row, "disposition": DISPOSITIONS.get(row["key"])})
    return {
        "repo": REPO,
        "engine_sha": engine_sha(script_text),
        "vocab": {
            k: sorted(v) if isinstance(v, (set, list)) else v for k, v in vocab.items()
        },
        "ignore": sorted(IGNORE),
        "units": units,
        "sites": found["sites"],
        "non_memory_sites": NON_MEMORY,
        "parse_errors": found["parse_errors"],
        "blind_spots": {
            "waits": spots["waits"],
            "wait_users": WAIT_USERS,
            "non_python": spots["non_python"],
        },
        "core_wait_imports": import_edges(
            repo, "smartmemory.testing.waits", ignore=IGNORE
        ),
        "owned_by_u4_42": OWNED_BY_U4_42,
        "deferred": [
            {
                key: u["disposition"].get(key)
                for key in ("class", "where", "owner", "prepared")
            }
            | {"key": u["key"]}
            for u in units
            if u["disposition"] and u["disposition"]["class"] in DEFERRED
        ],
    }


def render(doc):
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def problems(doc, repo):
    """Every reason the census is not trustworthy, as sentences. Empty means the census is complete and consistent."""
    repo, out = Path(repo), []
    keys = {u["key"] for u in doc["units"]}
    for key in sorted(set(DISPOSITIONS) - keys):
        out.append(
            f"disposition for {key} has no unit: the test or its save was removed or renamed"
        )
    for unit in doc["units"]:
        d = unit["disposition"]
        if d is None:
            out.append(
                f"{unit['key']} (line {unit['line']}) saves and has no disposition"
            )
            continue
        if d.get("class") not in CLASSES:
            out.append(f"{unit['key']}: unknown class {d.get('class')!r}")
            continue
        if not d.get("why"):
            out.append(f"{unit['key']}: a disposition needs a why")
        if d["class"] in DEFERRED:
            if not d.get("where") or not d.get("owner"):
                out.append(
                    f"{unit['key']}: a deferral names where (file:line) and its owner step"
                )
            prepared = d.get("prepared", "")
            try:
                call = ast.parse(prepared, mode="eval").body
                ok = isinstance(call, ast.Call) and _name(call.func) == "await_public"
                ok = ok and {k.arg for k in call.keywords} >= {"wait", "deadline_s"}
                ok = ok and any(
                    isinstance(a, ast.Call)
                    and _name(a.func) in ("observe_get", "observe_search")
                    for a in call.args
                )
            except SyntaxError:
                ok = False
            if not ok:
                out.append(
                    f"{unit['key']}: a deferral carries the prepared await_public(observe_get|observe_search(...), wait=, deadline_s=) call"
                )
            for where in d.get("where", []):
                path, _, line = where.rpartition(":")
                target = repo / path
                if not (
                    target.is_file()
                    and line.isdigit()
                    and 1 <= int(line) <= len(target.read_text().splitlines())
                ):
                    out.append(
                        f"{unit['key']}: where {where} is not a line of this tree"
                    )
        if d["class"] == "converted" and not d.get("wait_line"):
            out.append(
                f"{unit['key']}: converted needs the wait_line of its await_public call"
            )
        if d["class"] == "save_only" and unit["reads_after_save"]:
            out.append(
                f"{unit['key']}: classed save_only but a read follows the save (lines {unit['reads']})"
            )
    memory_less = {
        f"{s['file']}:{s['line']}" for s in doc["sites"] if not s["memory_like"]
    }
    for site in sorted(memory_less - set(NON_MEMORY)):
        out.append(
            f"{site}: a save-named call on a receiver that does not look like memory, name it in NON_MEMORY"
        )
    for site in sorted(set(NON_MEMORY) - memory_less):
        out.append(f"NON_MEMORY names {site}, which is not a non-memory save site now")
    for spot in sorted(
        {w.rsplit(":", 1)[0] for w in doc["blind_spots"]["waits"]} - set(WAIT_USERS)
    ):
        out.append(f"{spot} sleeps, polls or waits and is not named in WAIT_USERS")
    for user in sorted(
        set(WAIT_USERS) - {w.rsplit(":", 1)[0] for w in doc["blind_spots"]["waits"]}
    ):
        out.append(f"WAIT_USERS names {user}, which no longer waits")
    if doc["parse_errors"]:
        out.append(
            f"{len(doc['parse_errors'])} test file(s) could not be parsed: {doc['parse_errors']}"
        )
    if doc["core_wait_imports"]:
        out.append(
            f"the core wait helper is imported by an outer suite: {doc['core_wait_imports']}"
        )
    if OWNED_BY_U4_42 and (repo / OWNED_BY_U4_42["file"]).exists():
        out.append(
            f"{OWNED_BY_U4_42['file']} landed: convert the owned_by_u4_42 rows into dispositions and delete the list"
        )
    return out


def main(argv):
    root = Path(__file__).resolve().parents[1]
    doc = build(root)
    text = render(doc)
    fixture = root / FIXTURE
    if "--write" in argv:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(text)
        print(
            f"wrote {FIXTURE}: {len(doc['units'])} units, {len(doc['sites'])} save sites"
        )
    for line in problems(doc, root):
        print("PROBLEM:", line)
    if "--table" in argv:
        for u in doc["units"]:
            d = u["disposition"] or {"class": "MISSING"}
            print(
                f"{u['file']}:{u['line']} {u['function']} [{d['class']}] saves={[s[0] for s in u['saves']]}"
            )
    stale = not fixture.exists() or fixture.read_text() != text
    print("fixture " + ("STALE: run with --write" if stale else "current"))
    return 1 if stale or problems(doc, root) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
# --- COMMON END ---
