"""OPS-SERVICE-PREPUSH-PUSHED-SHA-1 (mcp twin): the pre-push hook checks the pushed sha, not HEAD, and finds siblings from a worktree.

push_round.py pushes 2.0 shas from a checkout that sits on main. The hook used to diff `<base>..HEAD` and read the
working tree, so it scanned main's files. Each row runs the real hook script as a subprocess against throwaway
sibling clones (bare local origins, no network).
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

GOOD_MAIN = "from service_common.auth.core import present"
BAD = "from service_common.auth.core import absent"
ONLY_2_0 = "from service_common.auth.core import only2"
CORE_GOOD = "from smartmemory.thing import present_core"
CORE_BAD = "from smartmemory.thing import absent_core"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(cwd)},
    ).stdout.strip()


def _sibling(
    tmp: Path, name: str, pkg: str, module: str, main_src: str, two_src: str | None = None, two_replaces: bool = False
) -> None:
    """tmp/<name>: a clone of a bare origin whose `main` (and optionally `2.0`) carries the given module source."""
    seed = tmp / f"seed-{name}"
    (seed / pkg).mkdir(parents=True)
    (seed / pkg / "__init__.py").write_text("")
    target = seed / pkg / f"{module}.py"
    if module == "core":  # service_common.auth.core
        (seed / pkg / "auth").mkdir()
        (seed / pkg / "auth" / "__init__.py").write_text("")
        target = seed / pkg / "auth" / "core.py"
    target.write_text(main_src)
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    if two_src is not None:
        _git(seed, "checkout", "-q", "-b", "2.0")
        target.write_text(two_src if two_replaces else main_src + two_src)
        _git(seed, "commit", "-qam", "two")
    _git(tmp, "clone", "-q", "--bare", str(seed), str(tmp / f"{name}-origin.git"))
    _git(tmp, "clone", "-q", str(tmp / f"{name}-origin.git"), str(tmp / name))


def _repo(tmp: Path, with_core: bool = True, diverged: bool = False) -> Path:
    """`diverged`: the common sibling's `main` has only `only_main` (plus `present`) and its `2.0` only `only2`."""
    if diverged:
        _sibling(
            tmp, "smart-memory-common", "service_common", "core", "present = 1\nonly_main = 1\n",
            "present = 1\nonly2 = 1\n", two_replaces=True,
        )
    else:
        _sibling(tmp, "smart-memory-common", "service_common", "core", "present = 1\n", "only2 = 1\n")
    if with_core:
        _sibling(tmp, "smart-memory-core", "smartmemory", "thing", "present_core = 1\n")
    repo = tmp / "smart-memory-mcp"
    (repo / "memory_service").mkdir(parents=True)
    (repo / "memory_service" / "__init__.py").write_text("")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/2.0", "HEAD")
    return repo


def _commit_on(repo: Path, branch: str, line: str) -> str:
    """Commit `memory_service/uses.py` with `line` on a new branch cut from main, return its sha."""
    _git(repo, "checkout", "-q", "-b", branch, "main")
    (repo / "memory_service" / "uses.py").write_text(line + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", branch)
    return _git(repo, "rev-parse", "HEAD")


def _hook(cwd: Path, stdin: str, home: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "PREPUSH_TARGET_BRANCH"}
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(HOOK)], cwd=cwd, input=stdin, env=env, capture_output=True, text=True, timeout=60
    )


def _push(sha: str, target: str = "main") -> str:
    return f"refs/heads/x {sha} refs/heads/{target} {'2' * 40}\n"


@pytest.mark.parametrize(
    "row_id,pushed,head,target,expected_exit",
    [
        ("pushed_sha_good_head_bad_passes", GOOD_MAIN, BAD, "main", 0),
        ("pushed_sha_bad_head_clean_blocked", BAD, GOOD_MAIN, "main", 1),
        ("pushed_sha_needs_sibling_2_0_passes", ONLY_2_0, BAD, "2.0", 0),
        ("pushed_sha_needs_sibling_2_0_but_target_main_blocked", ONLY_2_0, GOOD_MAIN, "main", 1),
    ],
)
def test_the_pushed_sha_is_scanned_not_head(tmp_path, row_id, pushed, head, target, expected_exit):
    repo = _repo(tmp_path)
    pushed_sha = _commit_on(repo, "pushed", pushed)
    _commit_on(repo, "other", head)  # the checkout stays on this branch, whose file differs
    done = _hook(repo, _push(pushed_sha, target), tmp_path)
    assert done.returncode == expected_exit, done.stderr
    assert ("BLOCKED" in done.stderr) is (expected_exit == 1), done.stderr
    assert "no pushed sha on stdin" not in done.stderr
    assert "could not fetch" not in done.stderr, done.stderr


def test_a_linked_worktree_finds_both_siblings_beside_the_main_checkout(tmp_path):
    repo = _repo(tmp_path)
    work = tmp_path / "elsewhere" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wk", str(work), "main")
    (work / "memory_service" / "uses.py").write_text(f"{GOOD_MAIN}\n{CORE_GOOD}\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "pushed")
    sha = _git(work, "rev-parse", "HEAD")
    assert not (work.parent / "smart-memory-core").exists()  # the old root-relative lookup finds nothing here

    done = _hook(work, _push(sha), tmp_path)
    assert done.returncode == 0, done.stderr
    assert "sibling not checked out" not in done.stderr, done.stderr

    (work / "memory_service" / "uses.py").write_text(f"{GOOD_MAIN}\n{CORE_BAD}\n")
    _git(work, "commit", "-qam", "bad core")
    done = _hook(work, _push(_git(work, "rev-parse", "HEAD")), tmp_path)
    assert done.returncode == 1, done.stderr  # the core sibling was really read, not skipped
    assert "from smartmemory.thing import absent_core" in done.stderr


def test_a_missing_sibling_names_the_path_tried(tmp_path):
    repo = _repo(tmp_path, with_core=False)
    work = tmp_path / "elsewhere" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wk", str(work), "main")
    (work / "memory_service" / "uses.py").write_text(GOOD_MAIN + "\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "pushed")
    done = _hook(work, _push(_git(work, "rev-parse", "HEAD")), tmp_path)
    assert done.returncode == 0, done.stderr
    assert f"sibling not checked out at {(tmp_path / 'smart-memory-core').resolve()}" in done.stderr, done.stderr


def test_no_stdin_falls_back_to_head_with_a_warning(tmp_path):
    repo = _repo(tmp_path)
    _commit_on(repo, "pushed", BAD)
    done = _hook(repo, "", tmp_path)
    assert done.returncode == 1, done.stderr  # HEAD is scanned, as before
    assert "no pushed sha on stdin: scanning HEAD" in done.stderr, done.stderr


def test_an_unresolvable_pushed_sha_falls_back_to_head_with_a_warning(tmp_path):
    repo = _repo(tmp_path)
    _commit_on(repo, "pushed", GOOD_MAIN)
    done = _hook(repo, _push("1" * 40), tmp_path)
    assert done.returncode == 0, done.stderr
    assert f"pushed sha {'1' * 40} not found in this repo: scanning HEAD instead" in done.stderr, done.stderr


ONLY_MAIN = "from service_common.auth.core import only_main"


def _mixed_stdin(sha_main: str, sha_20: str) -> str:
    return _push(sha_main, "main") + _push(sha_20, "2.0")


def test_a_mixed_push_checks_each_line_against_its_own_target(tmp_path):
    """OPS-SERVICE-PREPUSH-PER-LINE-TARGET-1: main imports a main-only symbol, 2.0 a 2.0-only one; each is clean alone."""
    repo = _repo(tmp_path, diverged=True)
    sha_main = _commit_on(repo, "m", ONLY_MAIN)
    sha_20 = _commit_on(repo, "t", ONLY_2_0)
    assert _hook(repo, _push(sha_main, "main"), tmp_path).returncode == 0
    assert _hook(repo, _push(sha_20, "2.0"), tmp_path).returncode == 0
    done = _hook(repo, _mixed_stdin(sha_main, sha_20), tmp_path)
    assert done.returncode == 0, done.stderr
    assert "BLOCKED" not in done.stderr, done.stderr


def test_a_mixed_push_still_blocks_a_line_that_misses_its_own_target(tmp_path):
    """The inverse: the main line imports a 2.0-only symbol, so it is blocked against `@origin/main` only."""
    repo = _repo(tmp_path, diverged=True)
    sha_main = _commit_on(repo, "m", ONLY_2_0)
    sha_20 = _commit_on(repo, "t", ONLY_2_0)
    done = _hook(repo, _mixed_stdin(sha_main, sha_20), tmp_path)
    assert done.returncode == 1, done.stderr
    assert "@origin/main" in done.stderr, done.stderr
    assert "@origin/2.0" not in done.stderr, done.stderr
