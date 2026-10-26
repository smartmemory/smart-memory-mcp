"""WS-ROUTING-1 S3: no client literal workspace defaults in the MCP package (WS-ROUTING-5).

A hard-coded workspace id is a client guessing where a write goes (the SDK's old `team_default_demo` fallback). Every
string constant in `smartmemory_mcp/` (docstrings and f-string parts included) is checked; field and key names such as
`team_id` are not ids and pass.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "smartmemory_mcp"

# `team_<x>` / `ws_<x>` where <x> is not a field-name suffix, anywhere in a string; the two known guesses always.
_ID_LITERAL = re.compile(
    r"\b(?:team_default_demo|ws_personal_\w*|(?:team|ws)_(?!(?:id|ids|name|names|slug|count|context)\b)[a-z0-9]\w*)",
    re.IGNORECASE,
)


def literal_ids(source: str) -> list[str]:
    """Every workspace-id-shaped literal in the string constants of `source`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(match.group(0) for match in _ID_LITERAL.finditer(node.value))
    return found


# The detector itself: source -> what it must report.
DETECTOR_ROWS = [
    pytest.param('X = "team_default_demo"', ["team_default_demo"], id="demo-default"),
    pytest.param(
        'h = {"X-Workspace-Id": "ws_personal_u1"}',
        ["ws_personal_u1"],
        id="personal-literal",
    ),
    pytest.param(
        'h = f"ws_personal_{uid}"', ["ws_personal_"], id="constructed-personal"
    ),
    pytest.param('t = "team_acme"', ["team_acme"], id="any-team-literal"),
    pytest.param('k = s["team_id"]; n = "team_name"', [], id="field-names-pass"),
    pytest.param('m = "steam_engine"', [], id="word-boundary"),
]


@pytest.mark.parametrize(("source", "expected"), DETECTOR_ROWS)
def test_the_detector(source: str, expected: list[str]) -> None:
    assert literal_ids(source) == expected


def test_the_mcp_package_has_no_literal_workspace_ids() -> None:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, f"no sources under {PACKAGE}"
    hits = {
        str(path.relative_to(PACKAGE)): ids
        for path in files
        if (ids := literal_ids(path.read_text(encoding="utf-8")))
    }
    assert hits == {}
