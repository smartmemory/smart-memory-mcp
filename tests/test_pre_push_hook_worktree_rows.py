"""OPS-PREPUSH-HOOK-WORKTREE-GITDIR-1: the pre-push hook gives the same answer from a linked worktree and a main checkout.

Git exports an absolute GIT_DIR (and GIT_WORK_TREE, GIT_INDEX_FILE) into hook processes. From a worktree those
pointed every `git -C <sibling>` call at THIS repo, so every symbol was reported missing from the sibling. Each row
runs the real hook script as a subprocess with the variables git would export, against a throwaway sibling clone.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = next(
    p / "scripts" / "hooks" / "pre-push"
    for p in Path(__file__).resolve().parents
    if (p / "scripts" / "hooks" / "pre-push").is_file()
)

SHA = "1" * 40
STDIN = f"refs/heads/main {SHA} refs/heads/main {'2' * 40}\n"
PRESENT = "from service_common.auth.core import present"
ABSENT = "from service_common.auth.core import absent"

# (row id, checkout layout, new import in the pushed commit, expected exit, BLOCKED text expected)
ROWS = [
    ("worktree_present_symbol_passes", "worktree", PRESENT, 0, False),
    ("worktree_absent_symbol_refused", "worktree", ABSENT, 1, True),
    ("main_present_symbol_passes", "main", PRESENT, 0, False),
    ("main_absent_symbol_refused", "main", ABSENT, 1, True),
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(cwd)},
    ).stdout.strip()


def _build(tmp_path: Path, layout: str, new_import: str) -> Path:
    """tmp/smart-memory-common (clone of a bare origin with `present`) next to the repo under test."""
    seed = tmp_path / "seed"
    (seed / "service_common" / "auth").mkdir(parents=True)
    (seed / "service_common" / "__init__.py").write_text("")
    (seed / "service_common" / "auth" / "__init__.py").write_text("")
    (seed / "service_common" / "auth" / "core.py").write_text("present = 1\n")
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "common")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(tmp_path / "common-origin.git"))
    _git(tmp_path, "clone", "-q", str(tmp_path / "common-origin.git"), str(tmp_path / "smart-memory-common"))

    repo = tmp_path / "repo"
    (repo / "memory_service").mkdir(parents=True)
    (repo / "memory_service" / "__init__.py").write_text("")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if layout == "worktree":
        work = tmp_path / "smart-memory-wt"
        _git(repo, "worktree", "add", "-q", "-b", "wk", str(work))
    else:
        work = repo
    (work / "memory_service" / "uses.py").write_text(new_import + "\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "pushed")
    return work


@pytest.mark.parametrize("row_id,layout,new_import,expected_exit,blocked", ROWS, ids=[r[0] for r in ROWS])
def test_hook_answer_is_the_same_in_worktree_and_main_checkout(
    tmp_path, row_id, layout, new_import, expected_exit, blocked
):
    work = _build(tmp_path, layout, new_import)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "PREPUSH_TARGET_BRANCH"}
    env["HOME"] = str(tmp_path)
    if layout == "worktree":  # git exports absolute paths from a linked worktree
        git_dir = _git(work, "rev-parse", "--absolute-git-dir")
        env.update(GIT_DIR=git_dir, GIT_WORK_TREE=str(work), GIT_INDEX_FILE=f"{git_dir}/index")
    else:  # and the relative `.git` from a main checkout, which resolves against any cwd
        env["GIT_DIR"] = ".git"

    done = subprocess.run(
        [sys.executable, str(HOOK)], cwd=work, input=STDIN, env=env, capture_output=True, text=True, timeout=60
    )

    assert done.returncode == expected_exit, done.stderr
    assert ("BLOCKED" in done.stderr) is blocked, done.stderr
    assert "could not fetch" not in done.stderr, done.stderr  # the sibling was really read, not skipped
    # The remedy never offers the bypass, and says to verify the symbol first.
    assert "--no-verify" not in done.stderr
    if blocked:
        assert "verify the symbol is really missing" in done.stderr
        assert "do not bypass" in done.stderr
