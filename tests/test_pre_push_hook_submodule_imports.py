"""PREPUSH-SUBMODULE-1: the smart-memory-mcp pre-push hook resolves `from pkg import name` when `name` is a submodule or subpackage.

One row per import shape, run through the hook's `main()` against a throwaway mcp repo and a sibling clone whose
origin/main holds the package tree. A name that exists only in the sibling's working tree is still refused.
"""

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"

# Sibling repo tree on origin/main (the hook maps `service_common` to ../smart-memory-common).
ORIGIN_TREE = {
    "service_common/__init__.py": "",
    "service_common/connectors/__init__.py": "defined = 1\nfrom .x import reexported\n",
    "service_common/connectors/x.py": "reexported = 1\n",
    "service_common/connectors/oauth_state.py": "token = 1\n",
    "service_common/connectors/subpkg/__init__.py": "",
    "service_common/ns/mod.py": "y = 1\n",  # namespace package: no __init__.py
}

# (row id, import line in the pushed file, exit code, why). Rows b, c, g, j failed before this unit; m and o failed at review round 1 (2ead3f7).
ROWS = [
    ("a_defined_in_init", "from service_common.connectors import defined", 0, ""),
    (
        "b_submodule_file",
        "from service_common.connectors import oauth_state",
        0,
        "hub-main report",
    ),
    ("c_subpackage", "from service_common.connectors import subpkg", 0, ""),
    ("d_reexported_in_init", "from service_common.connectors import reexported", 0, ""),
    ("e_missing_everywhere", "from service_common.connectors import nothing", 1, ""),
    (
        "f_dotted_import",
        "import service_common.connectors.oauth_state as ost",
        0,
        "plain imports are not validated",
    ),
    (
        "g_submodule_of_namespace_pkg",
        "from service_common.ns import mod",
        0,
        "no __init__.py to read",
    ),
    (
        "h_local_only_submodule",
        "from service_common.connectors import local_only",
        1,
        "only in sibling worktree",
    ),
    (
        "i_local_only_subpackage",
        "from service_common.connectors import local_pkg",
        1,
        "only in sibling worktree",
    ),
    (
        "j_submodule_of_top_package",
        "from service_common import connectors",
        0,
        "empty top __init__",
    ),
    (
        "k_mixed_names",
        "from service_common.connectors import defined, oauth_state",
        0,
        "",
    ),
    (
        "l_one_bad_name_in_mix",
        "from service_common.connectors import oauth_state, nothing",
        1,
        "",
    ),
    (
        "m_file_shadows_namespace_dir",
        "from service_common.plain import child",
        1,
        "review r1 H1: plain.py has no children",
    ),
    (
        "n_package_beats_file",
        "from service_common.both import child",
        0,
        "regular package wins over both.py",
    ),
    (
        "o_intermediate_shadowed",
        "from service_common.pkg.plain.sub import x",
        1,
        "pkg/plain.py shadows pkg/plain/sub",
    ),
    (
        "p_staged_only",
        "from service_common.connectors import staged_only",
        1,
        "staged, gone from the working tree",
    ),
    (
        "q_committed_locally_only",
        "from service_common.connectors import committed_only",
        1,
        "not on origin/main",
    ),
    (
        "r_nested_namespace",
        "from service_common.ns.deep import mod",
        0,
        "namespace under a namespace",
    ),
    (
        "s_name_in_file_module",
        "from service_common.connectors.oauth_state import token",
        0,
        "a file module binds the name",
    ),
    (
        "t_name_missing_in_file_module",
        "from service_common.connectors.oauth_state import nothing",
        1,
        "",
    ),
]

# Extra files on the sibling's origin/main for one row (pushed to its bare origin before the hook runs).
ORIGIN_EXTRA = {
    "m_file_shadows_namespace_dir": {
        "service_common/plain.py": "",
        "service_common/plain/child.py": "value = 1\n",
    },
    "n_package_beats_file": {
        "service_common/both.py": "",
        "service_common/both/__init__.py": "",
        "service_common/both/child.py": "value = 1\n",
    },
    "o_intermediate_shadowed": {
        "service_common/pkg/__init__.py": "",
        "service_common/pkg/plain.py": "",
        "service_common/pkg/plain/sub/__init__.py": "x = 1\n",
    },
    "r_nested_namespace": {"service_common/ns/deep/mod.py": "z = 1\n"},
}

# Sibling-clone state for one row: "staged" = `git add`, then delete the file; "committed" = commit in the clone only.
SIBLING_STATE = {
    "p_staged_only": ("staged", "service_common/connectors/staged_only.py"),
    "q_committed_locally_only": (
        "committed",
        "service_common/connectors/committed_only.py",
    ),
}


def _load_hook():
    loader = importlib.machinery.SourceFileLoader("pre_push_hook_mcp_sub", str(HOOK))
    spec = importlib.util.spec_from_loader("pre_push_hook_mcp_sub", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": str(cwd)},
    )


def _write_tree(root: Path, tree: dict[str, str]) -> None:
    for rel, text in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """tmp/service (origin/main = first commit) beside tmp/smart-memory-common (clone of a bare origin)."""
    bare = tmp_path / "common-origin.git"
    seed = tmp_path / "common-seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write_tree(seed, ORIGIN_TREE)
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "origin tree")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    _git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "smart-memory-common"))
    common = tmp_path / "smart-memory-common"
    # Names that exist only in the sibling's working tree, never on its origin/main.
    _write_tree(
        common,
        {
            "service_common/connectors/local_only.py": "z = 1\n",
            "service_common/connectors/local_pkg/__init__.py": "",
        },
    )

    service = tmp_path / "service"
    service.mkdir()
    _git(service, "init", "-q", "-b", "main")
    _write_tree(service, {"memory_service/__init__.py": ""})
    _git(service, "add", ".")
    _git(service, "commit", "-q", "-m", "base")
    _git(service, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.chdir(service)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    return service


@pytest.mark.parametrize(
    "row_id,import_line,expected_exit", [r[:3] for r in ROWS], ids=[r[0] for r in ROWS]
)
def test_submodule_import_resolution(
    workspace, capsys, row_id, import_line, expected_exit
):
    tmp_path = workspace.parent
    if row_id in ORIGIN_EXTRA:
        seed = tmp_path / "common-seed"
        _write_tree(seed, ORIGIN_EXTRA[row_id])
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", row_id)
        _git(seed, "push", "-q", str(tmp_path / "common-origin.git"), "main")
    if row_id in SIBLING_STATE:
        state, rel = SIBLING_STATE[row_id]
        common = tmp_path / "smart-memory-common"
        _write_tree(common, {rel: "v = 1\n"})
        _git(common, "add", rel)
        if state == "staged":
            (common / rel).unlink()
        else:
            _git(common, "commit", "-q", "-m", row_id)
    _write_tree(workspace, {"memory_service/uses.py": import_line + "\n"})
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "add import")

    code = _load_hook().main()

    err = capsys.readouterr().err
    assert code == expected_exit, err
    if expected_exit:
        assert "BLOCKED" in err
        assert "smart-memory-common@origin/main" in err
