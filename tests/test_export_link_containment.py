"""F4: MCP must preserve the core export destination refusal."""

import logging

import pytest

from smartmemory.tools.factory import create_lite_memory
from smartmemory.pipeline.config import PipelineConfig
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import portability_tools


@pytest.mark.parametrize("kind", ["destination", "parent", "archive"])
def test_tool_refuses_escaping_export_link_and_accepts_normal_path(
    tmp_path, monkeypatch, caplog, kind
):
    monkeypatch.setenv("SMARTMEMORY_CONFIG_DIR", str(tmp_path / "config"))
    memory = create_lite_memory(
        data_dir=str(tmp_path / "memory"),
        pipeline_profile=PipelineConfig.lite_hermetic(llm_enabled=False),
        spawn_worker=False,
    )
    backend = LocalBackend()
    backend._mem = memory
    monkeypatch.setattr(portability_tools, "get_backend", lambda: backend)
    registered = {}

    class Registry:
        def tool(self, **kwargs):
            def register(fn):
                registered[fn.__name__] = fn
                return fn

            return register

    portability_tools.register(Registry())
    export = registered["memory_export"]
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep external data")
    link = tmp_path / "out"
    link.symlink_to(outside, target_is_directory=True)
    target = link / "bundle" if kind == "parent" else link
    if kind == "archive":
        target = tmp_path / "bundle.tar.gz"
        target.symlink_to(sentinel)
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
            export(str(target))
        assert sentinel.read_text() == "keep external data"
        assert any(
            str(target) in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )
        normal = tmp_path / ("normal.tar.gz" if kind == "archive" else "normal")
        export(str(normal))
        assert (
            normal.is_file() if kind == "archive" else (normal / "index.md").is_file()
        )
    finally:
        memory.close()
