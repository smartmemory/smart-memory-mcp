"""OPS-PREPUSH-HOOK-DYNAMIC-EXPORTS-1: the pre-push hook resolves names a sibling module exports dynamically.

One row per export shape, run through the hook's `main(stdin_text)` against a throwaway service repo and a sibling
clone whose `service_common/auth/core.py` (plus any extra files of the row) has that shape. The pushed commit adds
one `from service_common.auth.core import <name>`. Rows that must stay refused prove the check did not go blind.
"""

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"

CORE = "service_common/auth/core.py"
BASE_TREE = {"service_common/__init__.py": "", "service_common/auth/__init__.py": ""}
TO_MAIN = f"refs/heads/main {'1' * 40} refs/heads/main {'2' * 40}\n"

# The LAND-GAP-1 shape, copied from common `service_common/auth/core.py:64` on origin/main.
LAND_GAP_GETATTR = '''
def get_auth_provider():
    return object()


def __getattr__(name):
    """Module-level __getattr__ for backward compat with `from core import auth_provider`."""
    if name == "auth_provider":
        return get_auth_provider()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
'''

# (row id, files of the sibling (CORE and extras), imported name, expected exit, substring the WARNING block must
#  contain (None = no WARNING), why)
ROWS = [
    (
        "static_def",
        {CORE: "def thing():\n    return 1\n"},
        "thing",
        0,
        None,
        "plain def",
    ),
    (
        "static_class",
        {CORE: "class Thing:\n    pass\n"},
        "Thing",
        0,
        None,
        "plain class",
    ),
    ("static_assign", {CORE: "thing = 1\n"}, "thing", 0, None, "plain assignment"),
    (
        "static_tuple_unpack",
        {CORE: "left, thing = 1, 2\n"},
        "thing",
        0,
        None,
        "unpacked target, was a false positive",
    ),
    (
        "static_import_as",
        {CORE: "import json as thing\n"},
        "thing",
        0,
        None,
        "`import x as y` binds y",
    ),
    (
        "static_conditional",
        {CORE: "try:\n    thing = 1\nexcept Exception:\n    thing = 2\n"},
        "thing",
        0,
        None,
        "try block",
    ),
    (
        "reexport_from_import",
        {
            CORE: "from service_common.auth.impl import thing\n",
            "service_common/auth/impl.py": "thing = 1\n",
        },
        "thing",
        0,
        None,
        "static re-export",
    ),
    (
        "reexport_relative_alias",
        {
            CORE: "from .impl import other as thing\n",
            "service_common/auth/impl.py": "other = 1\n",
        },
        "thing",
        0,
        None,
        "relative re-export with alias",
    ),
    (
        "reexport_star",
        {
            CORE: "from .impl import *\n",
            "service_common/auth/impl.py": "def thing():\n    return 1\n",
        },
        "thing",
        0,
        None,
        "star re-export",
    ),
    (
        "reexport_star_chain",
        {
            CORE: "from .mid import *\n",
            "service_common/auth/mid.py": "from .impl import *\n",
            "service_common/auth/impl.py": "thing = 1\n",
        },
        "thing",
        0,
        None,
        "star of a star",
    ),
    (
        "reexport_star_missing",
        {CORE: "from .impl import *\n", "service_common/auth/impl.py": "other = 1\n"},
        "thing",
        1,
        None,
        "star target lacks the name: still refused",
    ),
    (
        "reexport_star_private",
        {CORE: "from .impl import *\n", "service_common/auth/impl.py": "_thing = 1\n"},
        "_thing",
        1,
        None,
        "star import does not carry underscore names",
    ),
    (
        "reexport_star_cycle",
        {
            CORE: "from .impl import *\n",
            "service_common/auth/impl.py": "from .core import *\n",
        },
        "thing",
        1,
        None,
        "star cycle terminates and refuses",
    ),
    (
        "reexport_star_unresolvable",
        {CORE: "from os import *\n"},
        "thing",
        0,
        "cannot be resolved here",
        "star source outside the sibling: accepted with a WARNING",
    ),
    (
        "getattr_eq_land_gap",
        {CORE: LAND_GAP_GETATTR},
        "auth_provider",
        0,
        None,
        "LAND-GAP-1 Q1 auth_provider",
    ),
    (
        "getattr_in_tuple",
        {
            CORE: 'def __getattr__(name):\n    if name in ("a", "thing"):\n        return 1\n    raise AttributeError(name)\n'
        },
        "thing",
        0,
        None,
        "`name in (...)`",
    ),
    (
        "getattr_container_in",
        {
            CORE: '_LAZY = {"thing": "x.y"}\n\n\ndef __getattr__(name):\n    if name in _LAZY:\n        return 1\n'
            "    raise AttributeError(name)\n"
        },
        "thing",
        0,
        None,
        "lazy-export table",
    ),
    (
        "getattr_container_subscript",
        {
            CORE: '_LAZY = ["thing"]\n\n\ndef __getattr__(name):\n    return _LAZY[name]\n'
        },
        "thing",
        0,
        None,
        "`_LAZY[name]`",
    ),
    (
        "getattr_container_get",
        {
            CORE: '_LAZY = {"thing": 1}\n\n\ndef __getattr__(name):\n    return _LAZY.get(name)\n'
        },
        "thing",
        0,
        None,
        "`_LAZY.get(name)`",
    ),
    (
        "getattr_other_name_still_refused",
        {CORE: LAND_GAP_GETATTR},
        "not_auth_provider",
        1,
        None,
        "a __getattr__ that names auth_provider does not bless every name",
    ),
    (
        "getattr_container_other_name_refused",
        {
            CORE: '_LAZY = {"a": 1}\n\n\ndef __getattr__(name):\n    return _LAZY[name]\n'
        },
        "thing",
        1,
        None,
        "name absent from the lazy table",
    ),
    (
        "getattr_opaque_accepted_with_warning",
        {
            CORE: "import json\n\n\ndef __getattr__(name):\n    return getattr(json, name)\n"
        },
        "thing",
        0,
        "__getattr__ is opaque",
        "delegating __getattr__ cannot be listed: accepted with a WARNING",
    ),
    (
        "all_listed",
        {CORE: '__all__ = ["thing"]\n'},
        "thing",
        0,
        None,
        "`__all__` entry",
    ),
    (
        "all_augmented",
        {CORE: '__all__ = ["a"]\n__all__ += ["thing"]\n'},
        "thing",
        0,
        None,
        "`__all__ +=`",
    ),
    (
        "all_extend",
        {CORE: '__all__ = ["a"]\n__all__.extend(["thing"])\n'},
        "thing",
        0,
        None,
        "`__all__.extend`",
    ),
    (
        "all_other_name_refused",
        {CORE: '__all__ = ["a"]\n'},
        "thing",
        1,
        None,
        "name not in `__all__`",
    ),
    (
        "truly_missing",
        {CORE: "def other():\n    return 1\n"},
        "thing",
        1,
        None,
        "nothing exports it",
    ),
    (
        "missing_inside_function_only",
        {CORE: "def f():\n    thing = 1\n    return thing\n"},
        "thing",
        1,
        None,
        "local",
    ),
    (
        "missing_module",
        {CORE: "thing = 1\n"},
        "thing",
        1,
        None,
        "module absent (row overrides the import below)",
    ),
]
IMPORT_OVERRIDES = {"missing_module": "from service_common.auth.nowhere import thing"}


def _load_hook():
    loader = importlib.machinery.SourceFileLoader("pre_push_hook_dynamic", str(HOOK))
    spec = importlib.util.spec_from_loader("pre_push_hook_dynamic", loader)
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


def _build_common(tmp_path: Path, files: dict[str, str]) -> None:
    seed = tmp_path / "common-seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write_tree(seed, {**BASE_TREE, **files})
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "common main")
    bare = tmp_path / "common-origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    _git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "smart-memory-common"))


def _build_service(tmp_path: Path, new_import: str) -> Path:
    """origin/main = base, HEAD = base + uses.py (the pushed commit carrying the new import)."""
    service = tmp_path / "service"
    service.mkdir()
    _git(service, "init", "-q", "-b", "main")
    _write_tree(service, {"memory_service/__init__.py": ""})
    _git(service, "add", ".")
    _git(service, "commit", "-q", "-m", "base")
    _git(service, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write_tree(service, {"memory_service/uses.py": new_import + "\n"})
    _git(service, "add", ".")
    _git(service, "commit", "-q", "-m", "pushed")
    return service


@pytest.mark.parametrize(
    "row_id,files,name,expected_exit,warning,why",
    ROWS,
    ids=[r[0] for r in ROWS],
)
def test_dynamic_export_resolution(
    tmp_path, monkeypatch, capsys, row_id, files, name, expected_exit, warning, why
):
    new_import = IMPORT_OVERRIDES.get(
        row_id, f"from service_common.auth.core import {name}"
    )
    _build_common(tmp_path, files)
    service = _build_service(tmp_path, new_import)
    monkeypatch.chdir(service)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "PREPUSH_TARGET_BRANCH"):
        monkeypatch.delenv(var, raising=False)

    code = _load_hook().main(TO_MAIN)

    err = capsys.readouterr().err
    assert code == expected_exit, f"{why}\n{err}"
    assert ("BLOCKED" in err) == (expected_exit == 1), err
    if warning:
        assert "WARNING" in err and warning in err, err
    else:
        assert "WARNING" not in err, err
