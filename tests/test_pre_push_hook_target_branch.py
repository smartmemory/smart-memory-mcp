"""OPS-PREPUSH-HOOK-TARGET-BRANCH-1: the pre-push hook compares against the branch the push targets.

One row per push-target shape, run through the hook's `main(stdin_text)` against a throwaway mcp repo and a
sibling clone whose origin carries both `main` and `2.0`. The symbols differ per branch: `only_on_main` exists on
common main only, `get_write_scope_provider` on common 2.0 only (the LAND-GAP-1 false positive, common
`service_common/auth/core.py:318` on 2.0), `shared` on both.
"""

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"

CORE = "service_common/auth/core.py"
MAIN_TREE = {
    "service_common/__init__.py": "",
    "service_common/auth/__init__.py": "",
    CORE: "shared = 1\nonly_on_main = 1\n",
}
V2_TREE = {CORE: "shared = 1\n\n\ndef get_write_scope_provider():\n    return None\n"}

SHA = "1" * 40
OLD = "2" * 40
ZERO = "0" * 40
TO_MAIN = f"refs/heads/main {SHA} refs/heads/main {OLD}\n"
TO_V2 = f"refs/heads/2.0 {SHA} refs/heads/2.0 {OLD}\n"
TO_TAG = f"refs/tags/v1 {SHA} refs/tags/v1 {ZERO}\n"
TO_NEW_BRANCH = f"refs/heads/feature-x {SHA} refs/heads/feature-x {ZERO}\n"
DELETE_V2 = f"(delete) {ZERO} refs/heads/2.0 {OLD}\n"

IMP_MAIN_ONLY = "from service_common.auth.core import only_on_main"
IMP_V2_ONLY = "from service_common.auth.core import get_write_scope_provider"
IMP_SHARED = "from service_common.auth.core import shared"
IMP_GONE = "from service_common.auth.core import legacy_gone"

# (row id, hook stdin, new import in the pushed commit, env, expected exit, ref named in the BLOCKED text (or None),
#  substring the WARNING block must contain (or None = no WARNING), why)
ROWS = [
    ("main_target_main_symbol", TO_MAIN, IMP_MAIN_ONLY, {}, 0, None, None, ""),
    (
        "main_target_2_0_symbol",
        TO_MAIN,
        IMP_V2_ONLY,
        {},
        1,
        "origin/main",
        None,
        "main has no such name",
    ),
    (
        "2_0_target_2_0_symbol",
        TO_V2,
        IMP_V2_ONLY,
        {},
        0,
        None,
        None,
        "LAND-GAP-1 land-report3 false positive",
    ),
    (
        "2_0_target_main_only_symbol",
        TO_V2,
        IMP_MAIN_ONLY,
        {},
        1,
        "origin/2.0",
        None,
        "2.0 dropped the name",
    ),
    ("2_0_target_shared_symbol", TO_V2, IMP_SHARED, {}, 0, None, None, ""),
    (
        "unknown_no_stdin_main_symbol",
        "",
        IMP_MAIN_ONLY,
        {},
        0,
        None,
        "not a branch",
        "git invoked with no stdin",
    ),
    (
        "unknown_no_stdin_2_0_symbol",
        "",
        IMP_V2_ONLY,
        {},
        1,
        "origin/main",
        "not a branch",
        "falls back to main",
    ),
    ("tag_target", TO_TAG, IMP_MAIN_ONLY, {}, 0, None, "refs/tags/v1", "not a branch"),
    (
        "sibling_branch_missing_ok",
        TO_NEW_BRANCH,
        IMP_MAIN_ONLY,
        {},
        0,
        None,
        "no origin/feature-x",
        "common lacks it",
    ),
    (
        "sibling_branch_missing_bad",
        TO_NEW_BRANCH,
        IMP_V2_ONLY,
        {},
        1,
        "origin/main",
        "no origin/feature-x",
        "",
    ),
    (
        "own_baseline_is_target_branch",
        TO_V2,
        IMP_SHARED,
        {"LEGACY": IMP_GONE},
        0,
        None,
        None,
        "old import on 2.0",
    ),
    (
        "own_baseline_main_still_checked",
        TO_MAIN,
        IMP_SHARED,
        {"LEGACY": IMP_GONE},
        1,
        "origin/main",
        None,
        "r.o. main",
    ),
    (
        "delete_push_checks_nothing",
        DELETE_V2,
        IMP_GONE,
        {},
        0,
        None,
        None,
        "no local sha",
    ),
    (
        "multi_target_one_bad",
        TO_MAIN + TO_V2,
        IMP_V2_ONLY,
        {},
        1,
        "origin/main",
        None,
        "main target fails",
    ),
    ("multi_target_both_ok", TO_MAIN + TO_V2, IMP_SHARED, {}, 0, None, None, ""),
    (
        "kill_switch_legacy_main",
        TO_V2,
        IMP_V2_ONLY,
        {"PREPUSH_TARGET_BRANCH": "0"},
        1,
        "origin/main",
        None,
        "old rule",
    ),
]


def _load_hook():
    loader = importlib.machinery.SourceFileLoader("pre_push_hook_target", str(HOOK))
    spec = importlib.util.spec_from_loader("pre_push_hook_target", loader)
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


def _build_common(tmp_path: Path) -> None:
    """tmp/common-origin.git with `main` and `2.0` (2.0 drops only_on_main, adds get_write_scope_provider)."""
    seed = tmp_path / "common-seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write_tree(seed, MAIN_TREE)
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "common main")
    _git(seed, "checkout", "-q", "-b", "2.0")
    _write_tree(seed, V2_TREE)
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "common 2.0")
    bare = tmp_path / "common-origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    _git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "smart-memory-common"))


def _build_mcp(tmp_path: Path, legacy_import: str, new_import: str) -> Path:
    """origin/main = base, origin/2.0 = base + legacy.py, HEAD = origin/2.0 + uses.py (the pushed commit)."""
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    _git(mcp, "init", "-q", "-b", "main")
    _write_tree(mcp, {"memory_service/__init__.py": ""})
    _git(mcp, "add", ".")
    _git(mcp, "commit", "-q", "-m", "base")
    _git(mcp, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write_tree(mcp, {"memory_service/legacy.py": legacy_import + "\n"})
    _git(mcp, "add", ".")
    _git(mcp, "commit", "-q", "-m", "legacy")
    _git(mcp, "update-ref", "refs/remotes/origin/2.0", "HEAD")
    _write_tree(mcp, {"memory_service/uses.py": new_import + "\n"})
    _git(mcp, "add", ".")
    _git(mcp, "commit", "-q", "-m", "pushed")
    return mcp


@pytest.mark.parametrize(
    "row_id,stdin,new_import,env,expected_exit,blocked_ref,warning,why",
    ROWS,
    ids=[r[0] for r in ROWS],
)
def test_target_branch_resolution(
    tmp_path,
    monkeypatch,
    capsys,
    row_id,
    stdin,
    new_import,
    env,
    expected_exit,
    blocked_ref,
    warning,
    why,
):
    _build_common(tmp_path)
    mcp = _build_mcp(tmp_path, env.get("LEGACY", IMP_SHARED), new_import)
    monkeypatch.chdir(mcp)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "PREPUSH_TARGET_BRANCH"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        if key != "LEGACY":
            monkeypatch.setenv(key, value)

    code = _load_hook().main(stdin)

    err = capsys.readouterr().err
    assert code == expected_exit, err
    if blocked_ref:
        assert "BLOCKED" in err
        assert f"smart-memory-common@{blocked_ref}" in err
    else:
        assert "BLOCKED" not in err
    if warning:
        assert "WARNING" in err and warning in err, err
    else:
        assert "WARNING" not in err, err


def test_sibling_unreachable_is_skipped_not_blocked(tmp_path, monkeypatch, capsys):
    """Offline sibling: both the target branch and main fetches fail, so the check is skipped with a notice."""
    _build_common(tmp_path)
    mcp = _build_mcp(tmp_path, IMP_SHARED, IMP_V2_ONLY)
    _git(
        tmp_path / "smart-memory-common",
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "gone.git"),
    )
    monkeypatch.chdir(mcp)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "PREPUSH_TARGET_BRANCH"):
        monkeypatch.delenv(var, raising=False)

    assert _load_hook().main(TO_V2) == 0
    err = capsys.readouterr().err
    assert "could not fetch origin/2.0" in err
    assert "BLOCKED" not in err


# OPS-PREPUSH-BASE-FROM-MERGEBASE-1: a push target that is neither 2.0 nor main takes its base from the pushed commit.
# Shape: origin/main = base + main_tip.py, origin/2.0 = base + legacy.py (diverged), the pushed commit sits on top of
# the `cut_from` branch. `{sha}` in a stdin template is the pushed commit.
MB_TO_V2 = "refs/heads/2.0 {sha} refs/heads/2.0 " + OLD + "\n"
MB_TO_MAIN = "refs/heads/main {sha} refs/heads/main " + OLD + "\n"
MB_NEW_WK = "refs/heads/wk/x {sha} refs/heads/wk/x " + ZERO + "\n"
MB_TAG = "refs/tags/v1 {sha} refs/tags/v1 " + ZERO + "\n"
# (row id, cut from, stdin template, new import, env, expected exit, ref named in BLOCKED (or None), WARNING text (or None))
MB_ROWS = [
    ("push_2_0_off_2_0", "2.0", MB_TO_V2, IMP_V2_ONLY, {}, 0, None, None),
    ("push_main_off_main", "main", MB_TO_MAIN, IMP_MAIN_ONLY, {}, 0, None, None),
    ("new_wk_off_2_0_v2_symbol", "2.0", MB_NEW_WK, IMP_V2_ONLY, {}, 0, None, None),
    ("new_wk_off_2_0_main_symbol", "2.0", MB_NEW_WK, IMP_MAIN_ONLY, {}, 1, "origin/2.0", None),
    ("new_wk_off_main_main_symbol", "main", MB_NEW_WK, IMP_MAIN_ONLY, {}, 0, None, None),
    ("new_wk_off_main_v2_symbol", "main", MB_NEW_WK, IMP_V2_ONLY, {}, 1, "origin/main", None),
    ("tag_off_2_0", "2.0", MB_TAG, IMP_V2_ONLY, {}, 0, None, None),
    ("no_stdin_off_2_0", "2.0", "", IMP_V2_ONLY, {}, 1, "origin/main", "not a branch"),
    (
        "kill_switch_new_wk_off_2_0",
        "2.0",
        MB_NEW_WK,
        IMP_V2_ONLY,
        {"PREPUSH_BASE_FROM_MERGEBASE": "0"},
        1,
        "origin/main",
        "no origin/wk/x",
    ),
    (
        "old_kill_switch_still_main",
        "2.0",
        MB_NEW_WK,
        IMP_V2_ONLY,
        {"PREPUSH_TARGET_BRANCH": "0"},
        1,
        "origin/main",
        None,
    ),
]


def _build_diverged(tmp_path: Path, cut_from: str, new_import: str) -> tuple[Path, str]:
    """HEAD = origin/<cut_from> + uses.py, with origin/main and origin/2.0 diverged. Returns (repo, HEAD sha)."""
    repo = tmp_path / "mcp"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write_tree(repo, {"memory_service/__init__.py": ""})
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "v2")
    _write_tree(repo, {"memory_service/main_tip.py": IMP_SHARED + "\n"})
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main tip")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "v2")
    _write_tree(repo, {"memory_service/legacy.py": IMP_SHARED + "\n"})
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "legacy")
    _git(repo, "update-ref", "refs/remotes/origin/2.0", "HEAD")
    if cut_from == "main":
        _git(repo, "checkout", "-q", "main")
    _write_tree(repo, {"memory_service/uses.py": new_import + "\n"})
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pushed")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(repo)}
    ).stdout.strip()
    return repo, sha


@pytest.mark.parametrize(
    "row_id,cut_from,stdin,new_import,env,expected_exit,blocked_ref,warning",
    MB_ROWS,
    ids=[r[0] for r in MB_ROWS],
)
def test_base_from_merge_base(
    tmp_path, monkeypatch, capsys, row_id, cut_from, stdin, new_import, env, expected_exit, blocked_ref, warning
):
    _build_common(tmp_path)
    repo, sha = _build_diverged(tmp_path, cut_from, new_import)
    monkeypatch.chdir(repo)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "PREPUSH_TARGET_BRANCH", "PREPUSH_BASE_FROM_MERGEBASE"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    code = _load_hook().main(stdin.format(sha=sha))

    err = capsys.readouterr().err
    assert code == expected_exit, err
    if blocked_ref:
        assert f"smart-memory-common@{blocked_ref}" in err, err
    else:
        assert "BLOCKED" not in err, err
    if warning:
        assert "WARNING" in err and warning in err, err
    else:
        assert "WARNING" not in err, err
