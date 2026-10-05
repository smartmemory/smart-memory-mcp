"""Local MCP framework dead-code analysis on the real Lite store."""

import shutil

from fastmcp import FastMCP
from smartmemory.code.indexer import CodeIndexer
from smartmemory.pipeline.config import PipelineConfig
from smartmemory.tools.factory import create_lite_memory
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.hosted.tools import _CapturingRegistrar
from smartmemory_mcp.tools import code_tools, common


def test_local_dead_code_preserves_components_hooks_and_jsx(tmp_path, monkeypatch):
    root = tmp_path / "test_fw_checkout"
    data = tmp_path / "test_fw_store"
    root.mkdir()
    (root / "view.tsx").write_text("""
const Child = () => <span/>;
const Unused = () => <div/>;
function useUnused() { return 1; }
export const View = () => <Child/>;
""")
    memory = None
    try:
        memory = create_lite_memory(
            str(data),
            pipeline_profile=PipelineConfig.lite_hermetic(),
            spawn_worker=False,
        )
        indexer = CodeIndexer(memory._graph, "test_fw_repo", str(root))
        bundle, _ = indexer.prepare_bundle(["typescript"])
        assert indexer.publish_bundle(bundle, generate_embeddings=False).replaced
        local = LocalBackend()
        local._mem = memory
        monkeypatch.setattr(common, "_backend", local)
        registrar = _CapturingRegistrar(FastMCP("test_fw_local"))
        code_tools.register(registrar)
        output = registrar.captured["code_dead_code"].function("test_fw_repo")
        assert "Unused (component)" in output and "useUnused (hook)" in output
        assert "Child" not in output and "View" not in output
    finally:
        if memory is not None:
            memory.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(data, ignore_errors=True)
