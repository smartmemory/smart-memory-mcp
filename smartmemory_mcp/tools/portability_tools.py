"""Portability tools — export, import, and migrate OKF bundles between backends."""

from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from mcp.types import ToolAnnotations

from smartmemory_mcp.backends.local import LOCAL_WORKSPACE
from smartmemory_mcp.backends.remote import RemoteBackend

from .common import get_backend, graceful

logger = logging.getLogger(__name__)

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")
# Windows device names are unsafe on every host. Keep this check usable on
# Python 3.10+ without the deprecated PureWindowsPath.is_reserved API.
_WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{char}" for char in "123456789¹²³"}
    | {f"LPT{char}" for char in "123456789¹²³"}
)


# The live remote workspace a remote -> local migrate left behind, carried back by
# the next local -> remote migrate in this process (MCP-SWITCH-WORKSPACE-1 H1).
_parked: dict[str, str | None] = {"remote_workspace": None}


def _live_workspace(backend: Any) -> str | None:
    """The workspace id a remote backend's next request would send, or None."""
    workspace = getattr(backend, "active_workspace_id", None)
    return workspace if isinstance(workspace, str) and workspace else None


def _rebuild_process_backend(mode: str, workspace_id: str | None) -> None:
    """Point this process at ``mode`` and drop both backend caches together.

    A remote rebuild carries ``workspace_id`` (the live session's workspace);
    without one it starts from the configured default.
    """
    from smartmemory_mcp.backends import dispatch
    from smartmemory_mcp.tools.common import reset_backend as reset_common_backend

    with dispatch.rebuild_lock:
        dispatch.set_process_mode(mode, workspace_id=workspace_id)
        reset_common_backend()


def _leave_remote_workspace(live_workspace: str | None) -> str:
    """Park the live remote workspace on a move to local and say so."""
    _parked["remote_workspace"] = live_workspace
    if not live_workspace:
        return f"Workspace is now {LOCAL_WORKSPACE} (local mode)."
    logger.warning(
        "memory_migrate: remote workspace %s is not active in local mode (Lite has one "
        "workspace, %s); memory_migrate('remote') returns to it",
        live_workspace,
        LOCAL_WORKSPACE,
    )
    return (
        f"Workspace changed: now {LOCAL_WORKSPACE} (local mode has one workspace). "
        f"Remote workspace {live_workspace} is no longer active; "
        "memory_migrate('remote') returns to it."
    )


def _arrive_remote_workspace(backend: Any, carried: str | None) -> str:
    """Name the remote workspace an import landed in."""
    _parked["remote_workspace"] = None
    workspace = _live_workspace(backend) or "(none: the service default)"
    if carried:
        return f"Imported into workspace: {workspace} (your earlier remote workspace)."
    return f"Imported into workspace: {workspace}."


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
        if (
            path.is_absolute()
            or ".." in path.parts
            or PureWindowsPath(member.name).anchor
            or "\\" in member.name
            or any(
                any(char in part for char in '<>:"|?*')
                or any(ord(char) < 32 for char in part)
                or part.endswith((".", " "))
                or part.partition(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES
                for part in path.parts
            )
        ):
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
        root = destination.resolve()
        targets = []
        seen = set()
        # Validate the entire archive before publishing any member.
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError(f"Unsafe archive member path: {member.name}")
            identity = target.relative_to(destination).as_posix().casefold()
            if identity in seen:
                raise ValueError(f"Duplicate archive member path: {member.name}")
            seen.add(identity)
            targets.append(target)
        for member, target in zip(members, targets):
            if not target.resolve().is_relative_to(root):
                raise ValueError(f"Unsafe archive member path: {member.name}")
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

    @mcp.tool(
        title="Export a memory bundle",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_export(path: str) -> str:
        """Export all memories as an OKF bundle directory or tar.gz archive."""
        backend = get_backend()
        export_path = Path(path).expanduser().resolve()
        count = _export_okf(backend, export_path)
        if count == 0:
            return "No memories to export."
        return f"Exported {count} memories as an OKF bundle to {export_path}"

    @mcp.tool(
        title="Import a memory bundle",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
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

    @mcp.tool(
        title="Migrate memories to a backend",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @graceful
    def memory_migrate(target: str) -> str:
        """Migrate all memories to a different backend via a temporary OKF bundle."""
        if target not in ("local", "remote"):
            return f"Invalid target: {target}. Must be 'local' or 'remote'."

        from smartmemory_mcp.backends import dispatch

        # The MCP's backend follows SMARTMEMORY_MCP_MODE, not the config file, so the
        # source is whatever this process runs on; migrating onto itself would just
        # duplicate every memory.
        source_mode = dispatch.requested_mode()
        if source_mode == target:
            return (
                f"Already on the '{target}' backend ({dispatch.MODE_ENV}={source_mode}). "
                "Nothing to migrate."
            )

        source_backend = get_backend()
        # MCP-SWITCH-WORKSPACE-1 H1: an unsaved switch_workspace lives only on this
        # backend object. Capture it before any rebuild so every rebuild below can
        # carry it (or say why it cannot).
        live_workspace = (
            _live_workspace(source_backend)
            if source_mode == dispatch.MODE_REMOTE
            else None
        )
        temp_root = Path(tempfile.mkdtemp(prefix="smartmemory_migrate_"))
        bundle_path = temp_root / "bundle"
        try:
            count = _export_okf(source_backend, bundle_path)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        if count == 0:
            shutil.rmtree(temp_root, ignore_errors=True)
            return "No memories to migrate."

        try:
            from smartmemory_app.config import load_config, save_config

            # The file layer only: `load_config()` would carry SMARTMEMORY_TEAM_ID
            # (and every other env overlay) into the saved file (MCP-WORKSPACE-ENV-
            # PRECEDENCE-1).
            cfg = dispatch.file_layer_config(load_config)
            original_mode = cfg.mode
        except ImportError:
            shutil.rmtree(temp_root, ignore_errors=True)
            return "Config management requires the smartmemory package."

        cfg.mode = target
        save_config(cfg)
        # Remote -> local leaves the live workspace behind (Lite has one workspace);
        # local -> remote returns to the one parked by an earlier remote -> local.
        carried = (
            _parked["remote_workspace"] if target == dispatch.MODE_REMOTE else None
        )
        _rebuild_process_backend(target, carried)

        def revert() -> str:
            cfg.mode = original_mode
            save_config(cfg)
            # Read at revert time: a switch_workspace that ran during the export
            # mutated source_backend, and it is the caller's latest word. After the
            # forward rebuild nothing can reach source_backend, so this is its last
            # live value. (The park on success keeps the pre-export `live_workspace`:
            # that is where the exported data came from.)
            now = (
                _live_workspace(source_backend)
                if source_mode == dispatch.MODE_REMOTE
                else None
            )
            _rebuild_process_backend(source_mode, now)
            if source_mode == dispatch.MODE_LOCAL:
                return f"Still on workspace: {LOCAL_WORKSPACE} (local mode). "
            if now:
                return f"Still on workspace: {now}. "
            return ""

        try:
            target_backend = get_backend()
            result = _import_okf(target_backend, bundle_path)
            imported, failed = _import_counts(result)
            if failed > 0 and imported == 0:
                note = revert()
                return (
                    f"Migration failed: all {failed} items failed. "
                    f"Config reverted to '{original_mode}'. {note}Temp bundle: {bundle_path}"
                )

            shutil.rmtree(temp_root, ignore_errors=True)
            if target == dispatch.MODE_LOCAL:
                note = _leave_remote_workspace(live_workspace)
            else:
                note = _arrive_remote_workspace(target_backend, carried)
            return (
                f"Migration to '{target}' complete: {imported} succeeded, {failed} failed "
                f"out of {count} total. {note}"
            ).rstrip()
        except Exception as exc:
            note = revert()
            return (
                f"Migration failed: {exc}\nConfig reverted to '{original_mode}'. "
                f"{note}Temp bundle: {bundle_path}"
            )
