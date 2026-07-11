"""DB-free tests for OKF portability tool routing."""

from __future__ import annotations

import sys
import tarfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from smartmemory_mcp.backends.remote import RemoteBackend
from smartmemory_mcp.backends.local import LocalBackend
from smartmemory_mcp.tools import portability_tools


class FakeLocalBackend:
    """Local backend-shaped stub recording native OKF calls."""

    def __init__(self) -> None:
        self.export_paths: list[Path] = []
        self.import_paths: list[Path] = []

    def export_okf(self, path: str) -> int:
        bundle = Path(path)
        bundle.mkdir(parents=True)
        (bundle / "index.md").write_text("---\nokf_version: 0.1\n---\n")
        (bundle / "item.md").write_text("item")
        self.export_paths.append(bundle)
        return 1

    def import_okf(self, path: str) -> SimpleNamespace:
        self.import_paths.append(Path(path))
        return SimpleNamespace(imported=1, errors=0)


def _fake_remote() -> RemoteBackend:
    backend = object.__new__(RemoteBackend)
    backend.export_okf = MagicMock(side_effect=_write_remote_archive)
    backend.import_okf = MagicMock(return_value={"imported": 1, "failed": 0, "workspace_id": "ws-1"})
    return backend


def _write_remote_archive(path: str) -> None:
    archive_path = Path(path)
    source = archive_path.parent / "source"
    source.mkdir()
    (source / "index.md").write_text("---\nokf_version: 0.1\n---\n")
    (source / "item.md").write_text("item")
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.add(source, arcname="smartmemory-okf")


def _registered_tools() -> dict[str, object]:
    registered: dict[str, object] = {}

    class FakeMCP:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn

            return decorator

    portability_tools.register(FakeMCP())
    return registered


@pytest.fixture(autouse=True)
def _reset_backend_cache():
    from smartmemory_mcp.tools.common import reset_backend

    reset_backend()
    yield
    reset_backend()


def test_local_export_routes_bundle_directory(tmp_path: Path) -> None:
    backend = FakeLocalBackend()
    bundle_path = tmp_path / "bundle"

    count = portability_tools._export_okf(backend, bundle_path)

    assert count == 1
    assert backend.export_paths == [bundle_path]


def test_local_export_can_package_archive(tmp_path: Path) -> None:
    backend = FakeLocalBackend()
    archive_path = tmp_path / "bundle.tar.gz"

    count = portability_tools._export_okf(backend, archive_path)

    assert count == 1
    with tarfile.open(archive_path, mode="r:gz") as archive:
        assert "smartmemory-okf/index.md" in archive.getnames()


def test_remote_export_routes_archive_and_passes_active_backend(tmp_path: Path) -> None:
    backend = _fake_remote()
    archive_path = tmp_path / "bundle.tar.gz"

    count = portability_tools._export_okf(backend, archive_path)

    assert count == 1
    backend.export_okf.assert_called_once_with(str(archive_path))


def test_remote_export_can_expand_to_bundle_directory(tmp_path: Path) -> None:
    backend = _fake_remote()
    bundle_path = tmp_path / "bundle"

    count = portability_tools._export_okf(backend, bundle_path)

    assert count == 1
    assert (bundle_path / "smartmemory-okf" / "index.md").exists()


def test_remote_export_passes_active_workspace_to_headers(tmp_path: Path) -> None:
    backend = RemoteBackend(api_url="https://example.test", api_key="key", team_id="workspace-1")
    backend._session["_bootstrapped"] = True
    archive_path = tmp_path / "bundle.tar.gz"
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.iter_bytes.return_value = [b"archive"]

    with (
        patch.object(
            backend,
            "_headers",
            return_value={"Authorization": "Bearer key", "X-Workspace-Id": "workspace-1"},
        ) as build_headers,
        patch("smartmemory_mcp.backends.remote.httpx.stream", return_value=response),
    ):
        backend.export_okf(str(archive_path))

    build_headers.assert_called_once_with(workspace_id="workspace-1")
    assert archive_path.read_bytes() == b"archive"


def test_local_import_uses_direct_bundle_primitive(tmp_path: Path) -> None:
    backend = FakeLocalBackend()
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    (bundle_path / "index.md").write_text("index")

    result = portability_tools._import_okf(backend, bundle_path)

    assert result.imported == 1
    assert backend.import_paths == [bundle_path]


def test_local_backend_import_constructs_direct_corpus_importer(tmp_path: Path) -> None:
    backend = object.__new__(LocalBackend)
    backend._mem = object()
    importer = MagicMock()
    importer.run.return_value = SimpleNamespace(imported=1, errors=0)

    with patch("smartmemory.corpus.importer.CorpusImporter", return_value=importer) as importer_class:
        result = backend.import_okf(str(tmp_path))

    importer_class.assert_called_once_with(backend._mem, mode="direct")
    importer.run.assert_called_once_with(str(tmp_path))
    assert result.imported == 1


def test_remote_import_packages_bundle_before_upload(tmp_path: Path) -> None:
    backend = _fake_remote()
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    (bundle_path / "index.md").write_text("index")
    (bundle_path / "item.md").write_text("item")

    result = portability_tools._import_okf(backend, bundle_path)

    assert result["imported"] == 1
    uploaded = Path(backend.import_okf.call_args.args[0])
    assert uploaded.name == "bundle.tar.gz"


def test_remote_import_passes_active_workspace_to_headers_and_request(tmp_path: Path) -> None:
    backend = RemoteBackend(api_url="https://example.test", api_key="key", team_id="workspace-1")
    backend._session["_bootstrapped"] = True
    archive_path = tmp_path / "bundle.tar.gz"
    archive_path.write_bytes(b"archive")
    headers = {"Authorization": "Bearer key", "Content-Type": "application/json", "X-Workspace-Id": "workspace-1"}

    with (
        patch.object(backend, "_headers", return_value=headers) as build_headers,
        patch.object(
            backend,
            "_request",
            return_value={"imported": 1, "failed": 0, "workspace_id": "workspace-1"},
        ) as request,
    ):
        result = backend.import_okf(str(archive_path))

    build_headers.assert_called_once_with(workspace_id="workspace-1")
    assert request.call_args.kwargs["workspace_id"] == "workspace-1"
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer key", "X-Workspace-Id": "workspace-1"}
    assert result["workspace_id"] == "workspace-1"


def test_extract_bundle_rejects_parent_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape.md")
        info.size = 0
        archive.addfile(info)

    with pytest.raises(ValueError, match="Unsafe archive member path"):
        portability_tools._extract_bundle(archive_path, tmp_path / "out")

    assert not (tmp_path / "escape.md").exists()


def test_migrate_reverts_config_on_total_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _registered_tools()
    source_backend = object()
    target_backend = object()
    backends = iter([source_backend, target_backend])
    monkeypatch.setattr(portability_tools, "get_backend", lambda: next(backends))
    monkeypatch.setattr(portability_tools, "_export_okf", lambda backend, path: 2)
    monkeypatch.setattr(
        portability_tools,
        "_import_okf",
        lambda backend, path: {"imported": 0, "failed": 2},
    )

    cfg = SimpleNamespace(mode="local")
    saved_modes: list[str] = []
    config_module = types.ModuleType("smartmemory_app.config")
    config_module.load_config = lambda: cfg
    config_module.save_config = lambda current: saved_modes.append(current.mode)
    app_module = types.ModuleType("smartmemory_app")
    monkeypatch.setitem(sys.modules, "smartmemory_app", app_module)
    monkeypatch.setitem(sys.modules, "smartmemory_app.config", config_module)
    monkeypatch.setattr("smartmemory_mcp.backends.dispatch.reset_backend", lambda: None)
    monkeypatch.setattr("smartmemory_mcp.tools.common.reset_backend", lambda: None)

    result = tools["memory_migrate"]("remote")

    assert "Config reverted to 'local'" in result
    assert saved_modes == ["remote", "local"]


def test_migrate_rejects_invalid_target() -> None:
    result = _registered_tools()["memory_migrate"]("cloud")
    assert result == "Invalid target: cloud. Must be 'local' or 'remote'."
