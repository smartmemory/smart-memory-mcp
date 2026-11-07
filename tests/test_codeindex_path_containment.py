"""F2: the standalone Python parser never reads outside the selected checkout."""

import logging
import os
import builtins

import pytest

from smartmemory_mcp.code_parser import CodeParser, collect_python_files


def test_bundled_parser_refuses_escape_and_accepts_normal_file(tmp_path, caplog):
    root = tmp_path / "test_codeindex_checkout"
    root.mkdir()
    source = root / "normal.py"
    source.write_text("def normal():\n    return 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text(
        '"""test_codeindex_private_contents"""\ndef private_external():\n    pass\n'
    )
    link = root / "escape.py"
    link.symlink_to(outside)
    parser = CodeParser("test_codeindex", str(root))
    assert any(
        entity.name == "normal" for entity in parser.parse_file(str(source)).entities
    )
    with caplog.at_level(logging.WARNING):
        refused = parser.parse_file(str(link))
        errors = []
        files = collect_python_files(str(root), errors=errors)
    assert not refused.entities and not refused.relations
    assert refused.errors and "escape.py" in refused.errors[0]
    assert files == [str(source)]
    assert errors and "escape.py" in errors[0]
    assert "escape.py" in caplog.text
    assert "test_codeindex_private_contents" not in caplog.text
    assert "private_external" not in caplog.text


@pytest.mark.parametrize(
    "kind", ["fifo", "oversized", "directory_escape", "internal_link"]
)
def test_bundled_guard_handles_special_files_and_links(tmp_path, caplog, kind):
    root = tmp_path / "test_codeindex_checkout"
    root.mkdir()
    source = root / "normal.py"
    source.write_text("def normal():\n    return 1\n")
    entry = root / "entry.py"
    if kind == "internal_link":
        entry.symlink_to(source)
        assert any(
            e.name == "normal"
            for e in CodeParser("test_codeindex", str(root))
            .parse_file(str(entry))
            .entities
        )
        return
    if kind == "fifo":
        os.mkfifo(entry)
    elif kind == "oversized":
        with entry.open("wb") as handle:
            handle.truncate(2 * 1024 * 1024 + 1)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "hidden.py").write_text("def private_external():\n    pass\n")
        entry.symlink_to(outside, target_is_directory=True)
    with caplog.at_level(logging.WARNING):
        errors = []
        files = collect_python_files(str(root), errors=errors)
        result = CodeParser("test_codeindex", str(root)).parse_file(str(entry))
    assert not result.entities and result.errors
    assert errors and "entry.py" in errors[0]
    assert files == [str(source)]
    assert "entry.py" in caplog.text


def test_mcp_only_remote_caller_gets_refusal_without_upload(
    tmp_path, monkeypatch, caplog
):
    from smartmemory_mcp.backends.remote import RemoteBackend

    root = tmp_path / "test_codeindex_checkout"
    root.mkdir()
    (root / "normal.py").write_text("def normal():\n    return 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("def private_external():\n    pass\n")
    (root / "escape.py").symlink_to(outside)
    original_import = builtins.__import__

    def without_core(name, *args, **kwargs):
        if name == "smartmemory" or name.startswith("smartmemory."):
            raise ModuleNotFoundError(
                "No module named 'smartmemory'", name="smartmemory"
            )
        return original_import(name, *args, **kwargs)

    # An upload is forbidden for an incomplete checkout. This tripwire never supplies a fake response.
    def forbid_upload(*args, **kwargs):
        pytest.fail("refused checkout attempted an upload")

    backend = RemoteBackend(api_url="http://testserver")
    monkeypatch.setattr(builtins, "__import__", without_core)
    monkeypatch.setattr(backend, "request", forbid_upload)
    with caplog.at_level(logging.WARNING):
        result = backend.ingest_code(str(root), "test_codeindex")
    assert result.error_message and "escape.py" in result.error_message
    assert "Prior index was not changed" in result.error_message
    assert not result.replaced
    assert "escape.py" in caplog.text
