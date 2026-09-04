"""Portability tools — export, import, and migrate OKF bundles between backends."""

from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from smartmemory_mcp.backends.remote import RemoteBackend

from .common import get_backend, graceful

logger = logging.getLogger(__name__)

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")


def _is_archive(path: Path) -> bool:
    """Return whether a path names a supported compressed OKF archive."""
    return path.name.endswith(_ARCHIVE_SUFFIXES)


def _is_remote_backend(backend: Any) -> bool:
    """Return whether backend uses the hosted OKF archive transport."""
    return isinstance(backend, RemoteBackend)


def _bundle_page_count(bundle_dir: Path) -> int:
    """Count importable pages, excluding the reserved bundle index."""
    return sum(1 for page in bundle_dir.rglob("*.md") if page.name != "index.md")


def _archive_bundle(bundle_dir: Path, archive_path: Path) -> None:
    """Create a service-compatible tar.gz from an OKF bundle directory."""
    if not bundle_dir.is_dir():
        raise ValueError(f"OKF bundle path must be a directory: {bundle_dir}")
    for path in bundle_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"OKF bundles cannot contain symbolic links: {path}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.add(bundle_dir, arcname="smartmemory-okf")


def _validate_archive_members(members: list[tarfile.TarInfo]) -> None:
    """Reject archive members that could escape the extraction directory."""
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe archive member path: {member.name}")
        if not (member.isdir() or member.isreg()):
            raise ValueError(f"Unsupported archive member type: {member.name}")


def _extract_bundle(archive_path: Path, destination: Path) -> Path:
    """Safely extract an archive and return the directory containing index.md."""
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"OKF bundle destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        _validate_archive_members(members)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Unable to read archive member: {member.name}")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)

    indexes = list(destination.rglob("index.md"))
    if len(indexes) != 1:
        raise ValueError(
            f"OKF archive must contain exactly one index.md; found {len(indexes)}"
        )
    return indexes[0].parent


def _export_okf(backend: Any, output_path: Path) -> int:
    """Route an OKF export and return the number of item pages written."""
    if _is_remote_backend(backend):
        if _is_archive(output_path):
            backend.export_okf(str(output_path))
            with tempfile.TemporaryDirectory(
                prefix="smartmemory-okf-count-"
            ) as temp_dir:
                bundle_dir = _extract_bundle(output_path, Path(temp_dir))
                return _bundle_page_count(bundle_dir)
        with tempfile.TemporaryDirectory(
            prefix="smartmemory-okf-download-"
        ) as temp_dir:
            archive_path = Path(temp_dir) / "bundle.tar.gz"
            backend.export_okf(str(archive_path))
            bundle_dir = _extract_bundle(archive_path, output_path)
            return _bundle_page_count(bundle_dir)

    if _is_archive(output_path):
        with tempfile.TemporaryDirectory(prefix="smartmemory-okf-export-") as temp_dir:
            bundle_dir = Path(temp_dir) / "bundle"
            count = int(backend.export_okf(str(bundle_dir)))
            _archive_bundle(bundle_dir, output_path)
            return count
    return int(backend.export_okf(str(output_path)))


def _import_okf(backend: Any, input_path: Path) -> Any:
    """Route an OKF import, adapting bundle directories to archive transport."""
    if _is_remote_backend(backend):
        if _is_archive(input_path):
            return backend.import_okf(str(input_path))
        with tempfile.TemporaryDirectory(prefix="smartmemory-okf-upload-") as temp_dir:
            archive_path = Path(temp_dir) / "bundle.tar.gz"
            _archive_bundle(input_path, archive_path)
            return backend.import_okf(str(archive_path))

    if _is_archive(input_path):
        with tempfile.TemporaryDirectory(prefix="smartmemory-okf-import-") as temp_dir:
            bundle_dir = _extract_bundle(input_path, Path(temp_dir))
            return backend.import_okf(str(bundle_dir))
    return backend.import_okf(str(input_path))


def _import_counts(result: Any) -> tuple[int, int]:
    """Normalize local ImportStats and remote response counters."""
    if isinstance(result, dict):
        return int(result.get("imported", 0)), int(
            result.get("failed", result.get("errors", 0))
        )
    return int(getattr(result, "imported", 0)), int(getattr(result, "errors", 0))


def register(mcp: Any) -> None:
    """Register portability tools with the MCP server (3 tools)."""

    @mcp.tool()
    @graceful
    def memory_export(path: str) -> str:
        """Export all memories as an OKF bundle directory or tar.gz archive."""
        backend = get_backend()
        export_path = Path(path).expanduser().resolve()
        count = _export_okf(backend, export_path)
        if count == 0:
            return "No memories to export."
        return f"Exported {count} memories as an OKF bundle to {export_path}"

    @mcp.tool()
    @graceful
    def memory_import(path: str) -> str:
        """Losslessly import an OKF bundle through the direct add path."""
        backend = get_backend()
        import_path = Path(path).expanduser().resolve()
        if not import_path.exists():
            return f"Path not found: {import_path}"
        result = _import_okf(backend, import_path)
        imported, failed = _import_counts(result)
        return (
            f"Import complete: {imported} succeeded, {failed} failed from {import_path}"
        )

    @mcp.tool()
    @graceful
    def memory_migrate(target: str) -> str:
        """Migrate all memories to a different backend via a temporary OKF bundle."""
        if target not in ("local", "remote"):
            return f"Invalid target: {target}. Must be 'local' or 'remote'."

        from smartmemory_mcp.backends.dispatch import reset_backend
        from smartmemory_mcp.tools.common import reset_backend as reset_common_backend

        temp_root = Path(tempfile.mkdtemp(prefix="smartmemory_migrate_"))
        bundle_path = temp_root / "bundle"
        try:
            count = _export_okf(get_backend(), bundle_path)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        if count == 0:
            shutil.rmtree(temp_root, ignore_errors=True)
            return "No memories to migrate."

        try:
            from smartmemory_app.config import load_config, save_config

            cfg = load_config()
            original_mode = cfg.mode
        except ImportError:
            shutil.rmtree(temp_root, ignore_errors=True)
            return "Config management requires the smartmemory package."

        cfg.mode = target
        save_config(cfg)
        reset_backend()
        reset_common_backend()

        try:
            result = _import_okf(get_backend(), bundle_path)
            imported, failed = _import_counts(result)
            if failed > 0 and imported == 0:
                cfg.mode = original_mode
                save_config(cfg)
                reset_backend()
                reset_common_backend()
                return (
                    f"Migration failed: all {failed} items failed. "
                    f"Config reverted to '{original_mode}'. Temp bundle: {bundle_path}"
                )

            shutil.rmtree(temp_root, ignore_errors=True)
            return f"Migration to '{target}' complete: {imported} succeeded, {failed} failed out of {count} total."
        except Exception as exc:
            cfg.mode = original_mode
            save_config(cfg)
            reset_backend()
            reset_common_backend()
            return f"Migration failed: {exc}\nConfig reverted to '{original_mode}'. Temp bundle: {bundle_path}"
