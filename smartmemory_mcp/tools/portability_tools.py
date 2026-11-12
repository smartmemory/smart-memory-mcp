"""Portability tools — export, import, and migrate OKF bundles between backends."""

from __future__ import annotations

import gzip
import logging
import shutil
import stat
import tarfile
import tempfile
import zlib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from mcp.types import ToolAnnotations

from smartmemory_mcp.backends.local import LOCAL_WORKSPACE
from smartmemory_mcp.backends.remote import LIVE_PICK, RemoteBackend

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


def _leave_remote_workspace(live_workspace: str | None, latest: str | None) -> str:
    """Park the live remote workspace on a move to local and say so.

    ``live_workspace`` is the pick the export read from, so it is the one parked: a
    later ``memory_migrate('remote')`` must return the data where it came from.
    ``latest`` is the pick at the end, after any ``switch_workspace`` that committed
    during the export; it is not kept, and the reply and a WARNING say so
    (MCP-SWITCH-WORKSPACE-NAMING-SWEEP-1 item 8).
    """
    _parked["remote_workspace"] = live_workspace
    if not live_workspace:
        note = f"Workspace is now {LOCAL_WORKSPACE} (local mode)."
    else:
        logger.warning(
            "memory_migrate: remote workspace %s is not active in local mode (Lite has one "
            "workspace, %s); memory_migrate('remote') returns to it",
            live_workspace,
            LOCAL_WORKSPACE,
        )
        note = (
            f"Workspace changed: now {LOCAL_WORKSPACE} (local mode has one workspace). "
            f"Remote workspace {live_workspace} is no longer active; "
            "memory_migrate('remote') returns to it."
        )
    if latest and latest != live_workspace:
        source = live_workspace or "the workspace the service routed the export to"
        logger.warning(
            "memory_migrate: switch_workspace during the export was not kept: the migrated "
            "memories came from the workspace parked for memory_migrate('remote')"
        )
        note += (
            f" Your switch to {latest} during the export was not kept: the migrated "
            f"memories came from {source}. After memory_migrate('remote'), run "
            f"switch_workspace('{latest}') to use it."
        )
    return note


def _arrive_remote_workspace(backend: Any, carried: str | None) -> str:
    """Name the remote workspace an import landed in."""
    _parked["remote_workspace"] = None
    workspace = _live_workspace(backend) or "(none: the service default)"
    if carried:
        return f"Imported into workspace: {workspace} (your earlier remote workspace)."
    return f"Imported into workspace: {workspace}."


# Decoded-stream budget, twin of smart-memory-service ``portability.py`` (SVC-OKF-IMPORT-TAR-HEADER-BOMB-1; the
# parity row in tests/test_portability_tar_budget.py pins the same values). stdlib reads each PAX/GNU extension
# header whole before it returns a member, so a ~130 KB archive could make this process hold 512 MiB. Every decoded
# byte is charged against an allowance that grows only by what a well-formed tar spends: a fixed slack, one header
# block plus the padded data of each regular member, and each extension header (capped one by one, by chain length
# and in total). Unlike the service there is no cap on regular-file data here: a local bundle may be any size.
_TAR_BLOCK = tarfile.BLOCKSIZE
_READ_CHUNK = 1024 * 1024
_DECODED_SLACK_BYTES = 1024 * 1024
_MAX_EXTENSION_HEADER_BYTES = 64 * 1024
_MAX_EXTENSION_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_EXTENSION_CHAIN = 4
_MAX_GLOBAL_HEADERS = 8
_EXTENSION_TYPES = frozenset(
    {
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
    }
)


class ArchiveBudgetError(ValueError):
    """An OKF archive spends more decoded bytes on headers than its budget allows, or uses a refused member kind."""


def _padded(size: int) -> int:
    """Bytes a tar member body of ``size`` occupies on the stream, padded to whole blocks."""
    return -(-size // _TAR_BLOCK) * _TAR_BLOCK


class _DecodedBudget:
    """Allowance for one pass over one decoded tar stream; every decoded byte is charged against it."""

    def __init__(self) -> None:
        self.allowed = _DECODED_SLACK_BYTES
        self.decoded = 0
        self.extension_bytes = 0
        self.global_headers = 0
        self.chain = 0

    def charge(self, nbytes: int) -> None:
        self.decoded += nbytes
        if self.decoded > self.allowed:
            raise ArchiveBudgetError("OKF archive exceeds its decoded byte allowance")

    def member(self, member: tarfile.TarInfo) -> None:
        """Grant one member's header block and, for a regular file, its padded data, before that data is read."""
        if member.size < 0:
            raise ArchiveBudgetError("OKF archive member declares a negative size")
        if member.sparse is not None:
            raise ArchiveBudgetError("OKF archives cannot contain sparse members")
        self.allowed += _TAR_BLOCK + (_padded(member.size) if member.isreg() else 0)

    def extension(self, header: tarfile.TarInfo) -> None:
        """Check and grant a PAX or GNU long-name header from its declared size, before its body is read."""
        if not 0 <= header.size <= _MAX_EXTENSION_HEADER_BYTES:
            raise ArchiveBudgetError(
                "OKF archive extension header exceeds its size limit"
            )
        self.chain += 1
        if self.chain > _MAX_EXTENSION_CHAIN:
            raise ArchiveBudgetError("OKF archive chains too many extension headers")
        if header.type == tarfile.XGLTYPE:
            self.global_headers += 1
            if self.global_headers > _MAX_GLOBAL_HEADERS:
                raise ArchiveBudgetError(
                    "OKF archive carries too many global extension headers"
                )
        cost = _TAR_BLOCK + _padded(header.size)
        self.extension_bytes += cost
        if self.extension_bytes > _MAX_EXTENSION_TOTAL_BYTES:
            raise ArchiveBudgetError(
                "OKF archive extension headers exceed their total size limit"
            )
        self.allowed += cost


def _budgeted_tarinfo(budget: _DecodedBudget) -> type[tarfile.TarInfo]:
    """A TarInfo class whose header processing checks ``budget`` before stdlib reads any extension or sparse body."""

    class _BudgetedTarInfo(tarfile.TarInfo):
        # stdlib names _proc_member as the subclass hook: it runs once the 512-byte header is parsed, before any body.
        def _proc_member(self, archive):
            if self.type in _EXTENSION_TYPES:
                budget.extension(self)
            else:
                budget.chain = 0
                if self.type == tarfile.GNUTYPE_SPARSE:
                    raise ArchiveBudgetError(
                        "OKF archives cannot contain sparse members"
                    )
            return super()._proc_member(archive)

        def _proc_gnusparse_10(self, next, pax_headers, archive):
            # GNU sparse 1.0 reads its map from the data stream with no bound: refused before the first map byte.
            raise ArchiveBudgetError("OKF archives cannot contain sparse members")

    return _BudgetedTarInfo


class _BudgetedReader:
    """The gunzipped tar stream, charging every byte it returns to ``budget``; gzip failures become ``ReadError``."""

    def __init__(self, decoded: Any, budget: _DecodedBudget) -> None:
        self._decoded = decoded
        self._budget = budget

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > _READ_CHUNK:
            size = _READ_CHUNK
        try:
            data = self._decoded.read(size)
        except (OSError, EOFError, zlib.error) as exc:
            raise tarfile.ReadError(
                f"OKF archive is not a readable gzip stream: {exc}"
            ) from None
        self._budget.charge(len(data))
        return data


def _budgeted_members(
    archive_path: Path,
) -> Iterator[tuple[tarfile.TarFile, tarfile.TarInfo]]:
    """Yield ``(archive, member)`` for one streaming pass, with every decoded byte charged to a fresh budget.

    The member list is dropped at each step: stream mode would otherwise keep every TarInfo and its PAX dict.
    """
    budget = _DecodedBudget()
    with (
        archive_path.open("rb") as raw,
        gzip.GzipFile(fileobj=raw, mode="rb") as decoded,
        tarfile.open(
            fileobj=_BudgetedReader(decoded, budget),
            mode="r|",
            tarinfo=_budgeted_tarinfo(budget),
        ) as archive,
    ):
        while (member := archive.next()) is not None:
            del archive.members[:]
            budget.member(member)
            yield archive, member


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
    """Safely extract an archive and return the directory containing index.md.

    Both passes stream the archive under a decoded byte budget (``_DecodedBudget``), so headers, extension bodies
    and skipped data are bounded as well as the member paths.
    """
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"OKF bundle destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    seen = set()
    # Validate the entire archive before publishing any member.
    for _archive, member in _budgeted_members(archive_path):
        _validate_archive_members([member])
        target = destination.joinpath(*PurePosixPath(member.name).parts)
        if not target.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe archive member path: {member.name}")
        identity = target.relative_to(destination).as_posix().casefold()
        if identity in seen:
            raise ValueError(f"Duplicate archive member path: {member.name}")
        seen.add(identity)
    for archive, member in _budgeted_members(archive_path):
        target = destination.joinpath(*PurePosixPath(member.name).parts)
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


def _export_destination(path: Path) -> Path:
    """Preserve the selected spelling and refuse redirection before any writer runs."""
    path = path.expanduser().absolute()
    for entry in (path, *path.parents):
        if entry.is_symlink():
            logger.warning(
                "Refused export destination %s: symbolic link at %s", path, entry
            )
            raise ValueError(
                f"Refused export destination {path}: symbolic link at {entry}"
            )
    if path.exists():
        mode = path.stat().st_mode
        if not (stat.S_ISREG(mode) if _is_archive(path) else stat.S_ISDIR(mode)):
            logger.warning("Refused export destination %s: unsupported file type", path)
            raise ValueError(
                f"Refused export destination {path}: unsupported file type"
            )
    return path


def _export_okf(backend: Any, output_path: Path, workspace_id: Any = LIVE_PICK) -> int:
    """Route an OKF export and return the number of item pages written.

    ``workspace_id`` pins a remote export to a captured pick (see ``RemoteBackend.export_okf``).
    """
    output_path = _export_destination(output_path)
    if _is_remote_backend(backend):
        pin = {} if workspace_id is LIVE_PICK else {"workspace_id": workspace_id}
        if _is_archive(output_path):
            backend.export_okf(str(output_path), **pin)
            with tempfile.TemporaryDirectory(
                prefix="smartmemory-okf-count-"
            ) as temp_dir:
                bundle_dir = _extract_bundle(output_path, Path(temp_dir))
                return _bundle_page_count(bundle_dir)
        with tempfile.TemporaryDirectory(
            prefix="smartmemory-okf-download-"
        ) as temp_dir:
            archive_path = Path(temp_dir) / "bundle.tar.gz"
            backend.export_okf(str(archive_path), **pin)
            bundle_dir = _extract_bundle(archive_path, output_path)
            return _bundle_page_count(bundle_dir)

    if _is_archive(output_path):
        # Core refuses a symlinked export parent; macOS TMPDIR sits under /var
        # (-> private/var), so stage under the resolved temp dir.
        with tempfile.TemporaryDirectory(
            prefix="smartmemory-okf-export-",
            dir=Path(tempfile.gettempdir()).resolve(),
        ) as temp_dir:
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
        export_path = _export_destination(Path(path))
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
            # Pinned to the pick captured above: a switch_workspace that commits before the
            # export's headers are built must not change which workspace is read.
            count = _export_okf(
                source_backend,
                bundle_path,
                workspace_id=live_workspace
                if source_mode == dispatch.MODE_REMOTE
                else LIVE_PICK,
            )
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

        def revert() -> str:
            cfg.mode = original_mode
            save_config(cfg)
            # Read at revert time: a switch_workspace that committed during the
            # export mutated source_backend, and it is the caller's latest word.
            # After the forward rebuild nothing writes source_backend any more: the
            # stdio switch commits only onto the current process backend, under
            # rebuild_lock (`common.commit_if_current`), so one still resolving
            # across the rebuild is refused, not applied to this orphan
            # (MCP-SWITCH-INFLIGHT-REBUILD-1). The lock is held from the read to the
            # rebuild. (The park on success keeps the pre-export `live_workspace`:
            # that is where the exported data came from.)
            with dispatch.rebuild_lock:
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
            # Inside the try (review L2): a forward rebuild that raises, such as a
            # local -> remote build carrying the parked workspace, reverts the
            # config and the process mode like a failed import does.
            _rebuild_process_backend(target, carried)
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
                # Read after the forward rebuild: no switch commits onto
                # source_backend any more (`common.commit_if_current`).
                note = _leave_remote_workspace(
                    live_workspace, _live_workspace(source_backend)
                )
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
