"""Save-then-read census guard for the local and remote MCP suites (U49.A2, C-49.contract.mcp-suites; G11, G12, G47).

``scripts/save_then_read_census.py`` derives the save vocabulary from the backend classes and the registered tool
bodies, scans every test for the units that save, and holds one authored disposition per unit. These rows pin that
census: it is complete, the fixture is current, every defect is one named problem, each deferred unit's prepared
wait runs, and the units U4.42 will add are owned until its test file lands.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend
from tests.public_wait import await_public, observe_get

# --- SHARED BEGIN (byte-identical in smart-memory-client and smart-memory-mcp tests/test_save_then_read_census.py) ---
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "data" / "save_then_read_census.json"


def load_census():
    spec = importlib.util.spec_from_file_location(
        "save_then_read_census", ROOT / "scripts" / "save_then_read_census.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


census = load_census()
VOCAB = {
    "save_methods": {"add"},
    "save_tools": {"memory_save"},
    "read_tools": {"memory_get"},
}


def test_the_census_is_complete_and_the_fixture_is_current():
    doc = census.build(ROOT)
    assert census.problems(doc, ROOT) == []
    assert FIXTURE.read_text() == census.render(doc), (
        "run: python scripts/save_then_read_census.py --write"
    )


def test_every_deferral_names_its_prepared_call_where_and_owner():
    doc = json.loads(FIXTURE.read_text())
    assert doc["deferred"], "a census with no deferral must say why in the report"
    for item in doc["deferred"]:
        assert (
            item["where"] and item["owner"] and "await_public(" in item["prepared"]
        ), item["key"]


# id, source, expected {qualified function: reads_after_save}, non-memory save sites
ENGINE_ROWS = [
    (
        "a read after the save",
        "def test_a(memory):\n    memory.add('x')\n    memory.get('i')\n",
        {"test_a": True},
        0,
    ),
    (
        "a read before the save only",
        "def test_a(memory):\n    memory.get('i')\n    memory.add('x')\n",
        {"test_a": False},
        0,
    ),
    (
        "a save and no read",
        "def test_a(memory):\n    memory.add('x')\n",
        {"test_a": False},
        0,
    ),
    (
        "a save on a receiver that is not memory",
        "def test_b(seen):\n    seen.add('x')\n",
        {},
        1,
    ),
    (
        "a fixture consumer by parameter name",
        "@pytest.fixture\ndef seeded(memory):\n    memory.add('x')\n\n\ndef test_c(seeded, memory):\n    memory.get('i')\n",
        {"seeded": False, "test_c": True},
        0,
    ),
    (
        "an autouse fixture reaches the tests of its class",
        "class T:\n    @pytest.fixture(autouse=True)\n    def setup(self, client):\n        client.add('x')\n\n    def test_d(self):\n        self.client.get('i')\n",
        {"T.setup": False, "T.test_d": True},
        0,
    ),
    (
        "a helper is a unit and so is its caller",
        "def _seed(memory):\n    memory.add('x')\n\n\ndef test_e(memory):\n    _seed(memory)\n    memory.search('q')\n",
        {"_seed": False, "test_e": True},
        0,
    ),
    (
        "a tool name constant is a save and a read",
        "def test_f(call):\n    call('memory_save', {})\n    call('memory_get', {})\n",
        {"test_f": True},
        0,
    ),
    (
        "an attribute used as a value is a save",
        "def test_h(memory):\n    run = memory.add\n    run('x')\n    memory.get('i')\n",
        {"test_h": True},
        0,
    ),
    (
        "a getattr with a constant name is a save and a read",
        "def test_i(memory):\n    getattr(memory, 'add')('x')\n    getattr(memory, 'get')('i')\n",
        {"test_i": True},
        0,
    ),
    (
        "a getattr fed by a parametrize table is the methods the table names",
        "@pytest.mark.parametrize(('m', 'a'), [\n    ('get', 1),\n    ('add', 2),\n])\n"
        "def test_j(memory, m, a):\n    getattr(memory, m)(a)\n",
        {"test_j": True},
        0,
    ),
    (
        "a getattr fed by a module-level table",
        "ROWS = [('add', 1)]\n\n\n@pytest.mark.parametrize(('m', 'a'), ROWS)\n"
        "def test_k(memory, m, a):\n    getattr(memory, m)(a)\n",
        {"test_k": False},
        0,
    ),
    (
        "direct store setup is a save",
        "def test_l(store):\n    store.add_node('i', {})\n    store.get('i')\n",
        {"test_l": True},
        0,
    ),
    (
        "a publish into the store is a save",
        "def test_m(indexer, memory):\n    indexer.publish([], [])\n    memory.get('i')\n",
        {"test_m": True},
        0,
    ),
    (
        "a mock assertion on a save name is not a save",
        "def test_n(memory):\n    memory.add.assert_called_once()\n    memory.add.return_value = 1\n",
        {},
        0,
    ),
    (
        "a nested function is its own unit",
        "def test_g(memory):\n    def inner():\n        memory.add('x')\n\n    inner()\n",
        {"test_g.<locals>.inner": False, "test_g": False},
        0,
    ),
]


@pytest.mark.parametrize("row", ENGINE_ROWS, ids=[r[0] for r in ENGINE_ROWS])
def test_the_engine_finds_each_unit_shape(row, tmp_path):
    _, source, expected, outside = row
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(source)
    found = census.scan(tmp_path, VOCAB)
    assert {r["function"]: r["reads_after_save"] for r in found["rows"]} == expected
    assert sum(not s["memory_like"] for s in found["sites"]) == outside
    assert found["parse_errors"] == []


# id, files under tests/ (name: source), expected unit keys
IMPORT_ROWS = [
    (
        "a helper imported by name",
        {
            "helpers.py": "def seed(memory):\n    memory.add('x')\n",
            "test_a.py": "from tests.helpers import seed\n\n\ndef test_a(memory):\n    seed(memory)\n",
        },
        {"tests/helpers.py::seed", "tests/test_a.py::test_a"},
    ),
    (
        "a helper imported under another name",
        {
            "helpers.py": "def seed(memory):\n    memory.add('x')\n",
            "test_a.py": "from helpers import seed as plant\n\n\ndef test_a(memory):\n    plant(memory)\n",
        },
        {"tests/helpers.py::seed", "tests/test_a.py::test_a"},
    ),
    (
        "a helper reached through its module",
        {
            "helpers.py": "def seed(memory):\n    memory.add('x')\n",
            "test_a.py": "import tests.helpers as h\n\n\ndef test_a(memory):\n    h.seed(memory)\n",
        },
        {"tests/helpers.py::seed", "tests/test_a.py::test_a"},
    ),
    (
        "a fixture imported into a test module from another directory",
        {
            "lib/helpers.py": "@pytest.fixture\ndef seeded(memory):\n    memory.add('x')\n",
            "test_a.py": "from tests.lib.helpers import seeded\n\n\ndef test_a(seeded):\n    pass\n",
        },
        {"tests/lib/helpers.py::seeded", "tests/test_a.py::test_a"},
    ),
    (
        "a helper nobody imports does not make a caller",
        {
            "helpers.py": "def seed(memory):\n    memory.add('x')\n",
            "test_a.py": "def test_a(memory):\n    seed(memory)\n",
        },
        {"tests/helpers.py::seed"},
    ),
]


@pytest.mark.parametrize("row", IMPORT_ROWS, ids=[r[0] for r in IMPORT_ROWS])
def test_a_helper_defined_in_another_file_makes_its_importers_units(row, tmp_path):
    _, files, expected = row
    for name, source in files.items():
        path = tmp_path / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    found = census.scan(tmp_path, VOCAB)
    assert {r["key"] for r in found["rows"]} == expected


def test_a_getattr_no_literal_table_names_is_reported_not_ignored(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_a(memory, name):\n    getattr(memory, name)('x')\n\n\n"
        "def test_b(other, name):\n    getattr(other, name)('x')\n"
    )
    found = census.scan(tmp_path, VOCAB)
    assert found["dynamic"] == ["tests/test_x.py:2"], (
        "only a memory-like receiver is reported"
    )


# id, file bytes: a file the census cannot read as Python or text
UNREADABLE_ROWS = [
    ("a syntax error", b"def broken(:\n"),
    ("bytes that are not text", b"\xff\xfe def x(): pass\n"),
]


@pytest.mark.parametrize("row", UNREADABLE_ROWS, ids=[r[0] for r in UNREADABLE_ROWS])
def test_a_file_that_cannot_be_read_is_reported_by_scan_and_raises_nowhere(
    row, tmp_path
):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_bytes(row[1])
    found = census.scan(tmp_path, VOCAB)
    assert [e["file"] for e in found["parse_errors"]] == ["tests/test_x.py"]
    assert (
        census.import_edges(tmp_path, ".".join(("smartmemory", "testing", "waits")))
        == []
    )
    assert census.blind_spots(tmp_path)["waits"] == []


def _deferred(doc):
    return next(u for u in doc["units"] if u["disposition"]["class"] in census.DEFERRED)


def _first_with(doc, **kv):
    return next(u for u in doc["units"] if all(u[k] == v for k, v in kv.items()))


def _drop(unit, key):
    del unit["disposition"][key]


def _set(unit, key, value):
    unit["disposition"][key] = value


# id, mutation(doc, disp, monkeypatch), the one problem it must produce
PROBLEM_ROWS = [
    (
        "no disposition",
        lambda d, disp, mp: d["units"][0].update(disposition=None),
        "saves and has no disposition",
    ),
    (
        "a disposition whose unit is gone",
        lambda d, disp, mp: disp.update(
            {"tests/gone.py::test_x": {"class": "save_only", "why": "w"}}
        ),
        "has no unit",
    ),
    (
        "an unknown class",
        lambda d, disp, mp: _set(d["units"][0], "class", "bogus"),
        "unknown class",
    ),
    ("no why", lambda d, disp, mp: _set(d["units"][0], "why", ""), "needs a why"),
    (
        "a deferral without an owner",
        lambda d, disp, mp: _drop(_deferred(d), "owner"),
        "names where",
    ),
    (
        "a deferral owned by a description, not an id",
        lambda d, disp, mp: _set(_deferred(d), "owner", "the later-ring suite"),
        "is not a graph step id or a filed unit id",
    ),
    (
        "a deferral owner that is an uppercase description whose first word looks like an id",
        lambda d, disp, mp: _set(_deferred(d), "owner", "NOT AN OWNER"),
        "is not a graph step id or a filed unit id",
    ),
    (
        "a deferral owner that is a real owner id with trailing words",
        lambda d, disp, mp: _set(_deferred(d), "owner", "C49-RW-HARNESS-1 extra"),
        "is not a graph step id or a filed unit id",
    ),
    (
        "a deferral owner that is a lowercased unit id",
        lambda d, disp, mp: _set(_deferred(d), "owner", "c49-rw-harness-1"),
        "is not a graph step id or a filed unit id",
    ),
    (
        "a deferral owner that is an empty owner",
        lambda d, disp, mp: _set(_deferred(d), "owner", ""),
        "names where",
    ),
    (
        "a deferral owner that is a bare letter",
        lambda d, disp, mp: _set(_deferred(d), "owner", "X"),
        "is not a graph step id or a filed unit id",
    ),
    (
        "a deferral pointing at a missing line",
        lambda d, disp, mp: _set(_deferred(d), "where", ["tests/nope.py:1"]),
        "is not a line of this tree",
    ),
    (
        "a deferral without the prepared wait",
        lambda d, disp, mp: _set(_deferred(d), "prepared", "client.get('m-1')"),
        "prepared await_public",
    ),
    (
        "a deferral whose prepared text is not Python",
        lambda d, disp, mp: _set(_deferred(d), "prepared", "await_public("),
        "prepared await_public",
    ),
    (
        "converted without its wait line",
        lambda d, disp, mp: _set(d["units"][0], "class", "converted"),
        "converted needs the wait_line",
    ),
    (
        "save_only with a read after the save",
        lambda d, disp, mp: _set(
            _first_with(d, reads_after_save=True), "class", "save_only"
        ),
        "classed save_only but a read follows",
    ),
    (
        "a save on a non-memory receiver nobody named",
        lambda d, disp, mp: d["sites"].append(
            {"file": "tests/x.py", "line": 1, "memory_like": False}
        ),
        "does not look like memory",
    ),
    (
        "a NON_MEMORY entry that is no longer a site",
        lambda d, disp, mp: mp.setattr(
            census, "NON_MEMORY", [*census.NON_MEMORY, "tests/x.py:9"]
        ),
        "not a non-memory save site",
    ),
    (
        "a waiter nobody named",
        lambda d, disp, mp: d["blind_spots"]["waits"].append("tests/new_wait.py:3"),
        "not named in WAIT_USERS",
    ),
    (
        "a WAIT_USERS entry that no longer waits",
        lambda d, disp, mp: mp.setattr(
            census, "WAIT_USERS", {**census.WAIT_USERS, "tests/old.py": "r"}
        ),
        "no longer waits",
    ),
    (
        "a dispatch nobody named",
        lambda d, disp, mp: d["blind_spots"]["dynamic_dispatch"].update(
            {"tests/x.py:1": None}
        ),
        "name it in DYNAMIC",
    ),
    (
        "a DYNAMIC entry that is no longer a dispatch",
        lambda d, disp, mp: mp.setattr(
            census, "DYNAMIC", {**census.DYNAMIC, "tests/old.py:2": "r"}
        ),
        "not an unresolved dispatch now",
    ),
    (
        "a test file that cannot be parsed",
        lambda d, disp, mp: d.update(
            parse_errors=[{"file": "tests/x.py", "error": "e"}]
        ),
        "could not be parsed",
    ),
    (
        "an outer suite importing the core wait helper",
        lambda d, disp, mp: d.update(core_wait_imports=["tests/x.py:1"]),
        "core wait helper",
    ),
]


@pytest.mark.parametrize("row", PROBLEM_ROWS, ids=[r[0] for r in PROBLEM_ROWS])
def test_each_census_defect_is_one_named_problem(row, monkeypatch):
    _, mutate, expected = row
    doc = copy.deepcopy(census.build(ROOT))
    disp = copy.deepcopy(census.DISPOSITIONS)
    monkeypatch.setattr(census, "DISPOSITIONS", disp)
    for unit in doc["units"]:
        unit["disposition"] = disp[unit["key"]]
    mutate(doc, disp, monkeypatch)
    found = census.problems(doc, ROOT)
    assert len(found) == 1 and expected in found[0], found


# every owner in use or filed, and each form the census assigns: all whole-string ids, so none may raise a problem
OWNER_OK_ROWS = [
    "C49-RW-HARNESS-1",
    "C49-SDK-STATUS-1",
    "C-49.contract.sdk-suites",
    "C-49.contract.mcp-suites",
    "C-01.activate.save.hosted-adapters",
    "U49.A2",
]


@pytest.mark.parametrize("owner", OWNER_OK_ROWS)
def test_a_whole_string_owner_id_is_accepted(owner, monkeypatch):
    doc = copy.deepcopy(census.build(ROOT))
    disp = copy.deepcopy(census.DISPOSITIONS)
    monkeypatch.setattr(census, "DISPOSITIONS", disp)
    for unit in doc["units"]:
        unit["disposition"] = disp[unit["key"]]
    _set(_deferred(doc), "owner", owner)
    assert census.problems(doc, ROOT) == []


def test_every_owner_the_census_assigns_is_a_whole_string_id():
    owners = {d["owner"] for d in census.DISPOSITIONS.values() if "owner" in d}
    assert owners and all(census.OWNER_ID.fullmatch(o) for o in owners), owners


def test_the_command_line_passes_on_this_tree_and_fails_on_a_stale_fixture(
    monkeypatch, capsys
):
    assert census.main([]) == 0
    assert "fixture current" in capsys.readouterr().out
    monkeypatch.setattr(census, "render", lambda doc: "stale\n")
    assert census.main([]) == 1
    assert "STALE" in capsys.readouterr().out


def test_the_engine_is_pinned_by_its_hash():
    doc = json.loads(FIXTURE.read_text())
    assert doc["engine_sha"] == census.engine_sha(
        (ROOT / "scripts" / "save_then_read_census.py").read_text()
    )
    assert census.engine_sha(
        "# --- ENGINE BEGIN ---\na\n# --- ENGINE END ---"
    ) != census.engine_sha("# --- ENGINE BEGIN ---\nb\n# --- ENGINE END ---")


def test_the_ignored_files_are_the_censuss_own_and_visible_in_the_fixture():
    doc = json.loads(FIXTURE.read_text())
    assert doc["ignore"] == sorted(census.IGNORE)
    assert all((ROOT / path).is_file() for path in census.IGNORE)


# --- SHARED END ---


SAVE_METHODS = {
    "add",
    "add_with_receipt",
    "import_okf",
    "ingest",
    "ingest_code",
    "ingest_conversation_sync",
    "ingest_conversation_with_receipt",
    "ingest_document",
    "ingest_document_with_receipt",
    "ingest_structured",
    "ingest_structured_with_receipt",
    "ingest_with_receipt",
    "save",
    "save_call",
    "submit_save",
}
SAVE_TOOLS = {
    "code_index",
    "dev_log_friction",
    "dev_record_decision",
    "dev_record_pattern",
    "dev_save_session",
    "memory_save",
    "memory_save_conversation",
    "memory_save_document",
}


def test_the_derived_vocabulary_is_the_fixture_vocabulary_and_the_product_saves():
    derived = census.derive_vocab(ROOT)
    assert json.loads(json.dumps(derived)) == json.loads(FIXTURE.read_text())["vocab"]
    assert (set(derived["save_methods"]), set(derived["save_tools"])) == (
        SAVE_METHODS,
        SAVE_TOOLS,
    )


def test_a_new_backend_save_and_its_tool_join_the_vocabulary_without_an_edit(tmp_path):
    backends = tmp_path / "smartmemory_mcp" / "backends"
    backends.mkdir(parents=True)
    (backends / "interface.py").write_text(
        "class MemoryBackend:\n    def save_call(self): ...\n    def get(self): ...\n"
    )
    (backends / "local.py").write_text("class LocalBackend: ...\n")
    (backends / "remote.py").write_text("class RemoteBackend: ...\n")
    tools = tmp_path / "smartmemory_mcp" / "tools"
    tools.mkdir()
    (tools / "t.py").write_text(
        "def register(mcp):\n"
        "    @mcp.tool\n    def tool_save():\n        return get_backend().save_call()\n"
        "    @mcp.tool\n    def tool_read():\n        backend = get_backend()\n        return backend.get('x')\n"
        "    @mcp.tool\n    def tool_other():\n        return 1\n"
    )
    derived = census.derive_vocab(tmp_path)
    assert (derived["save_methods"], derived["save_tools"], derived["read_tools"]) == (
        ["save_call"],
        ["tool_save"],
        ["tool_read"],
    )


def test_the_static_u4_42_list_is_retired_and_its_file_units_have_dispositions():
    assert census.OWNED_BY_U4_42 == {}
    assert (ROOT / "tests/test_local_save_call.py").exists()
    doc = census.build(ROOT)
    assert doc["owned_by_u4_42"] == {}
    owned = [
        u
        for u in doc["units"]
        if u["key"].startswith("tests/test_local_save_call.py::")
    ]
    assert len(owned) == 13 and all(u["disposition"]["class"] for u in owned)
    assert census.problems(doc, ROOT) == []


def test_each_prepared_wait_runs_against_the_public_helpers():
    carrier = {
        "extraction_state": "extraction_pending",
        "processing_stage": "extracted",
        "execution_state": "completed",
        "outcome": "completed",
        "revision": 1,
    }
    backend = RemoteBackend(
        api_url="https://api.test", api_key="sk_test", team_id="ws-1"
    )
    backend._request = lambda *args, **kwargs: {
        "item_id": "m-1",
        "content": "x",
        "memory_type": "semantic",
        "read_status": carrier,
    }
    names = {"backend": backend, "item_id": "m-1"}
    names |= {"await_public": await_public, "observe_get": observe_get}
    for item_row in json.loads(FIXTURE.read_text())["deferred"]:
        result = eval(
            item_row["prepared"], dict(names)
        )  # the prepared text is data we authored, run it once
        assert (result.outcome, result.attempts) == ("reached", 1), item_row["key"]
