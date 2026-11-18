"""G9: registered filesystem tool uses the real compiler, never a hosted disk."""

import logging

import pytest
from fastmcp import FastMCP

from smartmemory_mcp.hosted import identity
from smartmemory_mcp.hosted.tools import HOSTED_TOOLS, _CapturingRegistrar
from smartmemory_mcp.tools import pattern_tools


@pytest.fixture
def tool():
    registrar = _CapturingRegistrar(FastMCP("test_g9_patterns"))
    pattern_tools.register(registrar)
    return registrar.captured["memory_get_violation_patterns"].function


def write_rule(root, filename, name, pattern):
    (root / filename).write_text(
        f"---\nname: {name}\n---\n## Detection patterns\n\n```yaml\npatterns:\n  - {pattern}\n```\n"
    )


def test_registered_tool_compiles_and_logs_unusable_rule_names(tool, tmp_path, caplog):
    write_rule(tmp_path, "feedback_good.md", "Good", "phrase: stop")
    write_rule(tmp_path, "feedback_bad.md", "Broken", "regex: '['")
    with caplog.at_level(logging.WARNING):
        result = tool(memory_dir=str(tmp_path))
    assert [r["name"] for r in result] == ["Good"]
    assert result[0]["patterns"] == [{"phrase": "stop"}]
    assert "Broken" in caplog.text
    assert tool(memory_dir=str(tmp_path), rule_id="Good") == result


def test_tool_does_not_read_sibling_directory_or_symlink(tool, tmp_path, caplog):
    a, b = tmp_path / "test_a", tmp_path / "test_b"
    a.mkdir()
    b.mkdir()
    write_rule(a, "feedback_a.md", "A", "phrase: a")
    write_rule(b, "feedback_b.md", "B", "phrase: b")
    (a / "feedback_escape.md").symlink_to(b / "feedback_b.md")
    with caplog.at_level(logging.WARNING):
        assert [r["name"] for r in tool(memory_dir=str(a))] == ["A"]
    assert "feedback_escape" in caplog.text
    assert "Refused" in tool(memory_dir=str(a), rule_type="../*")


def test_tool_missing_selection_is_explicit(tool, monkeypatch, caplog):
    monkeypatch.delenv("SMARTMEMORY_RULES_DIR", raising=False)
    with caplog.at_level(logging.WARNING):
        assert "No memory_dir resolved" in tool()
    assert "coverage unavailable" in caplog.text


def test_missing_core_compiler_warns_before_graceful_install_message(
    tool, monkeypatch, caplog
):
    import builtins

    original_import = builtins.__import__

    def without_core(name, *args, **kwargs):
        if name == "smartmemory.adherence":
            raise ImportError("test core unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_core)
    with caplog.at_level(logging.WARNING):
        assert "requires the smartmemory package" in tool()
    assert "core compiler unavailable" in caplog.text


def test_hosted_refusal_precedes_even_directory_resolution(tool, tmp_path, monkeypatch):
    assert "memory_get_violation_patterns" not in HOSTED_TOOLS
    # A bad prefix would raise if the filesystem compiler was reached.
    identity.set_hosted_mode(True)
    try:
        assert "hosted mode" in tool(memory_dir=str(tmp_path), rule_type="../*")
    finally:
        identity.set_hosted_mode(False)
