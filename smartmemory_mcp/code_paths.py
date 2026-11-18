"""Bounded checkout reads for the MCP-only parser, with no core dependency."""

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def read_python_source(path: str, root: str, *, read: bool = True) -> bytes:
    """Open only a regular, bounded source beneath the selected root.

    Resolve in-root links, then anchor each opened component at a directory descriptor using O_NOFOLLOW.
    Collection validates without reading bytes. Parsing repeats validation at the actual read boundary.
    """
    directory_fd = file_fd = None
    try:
        checkout = Path(root).resolve()
        root_info = checkout.stat()
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(checkout):
            raise ValueError("resolves outside the checkout root")
        parts = resolved.relative_to(checkout).parts
        if not parts:
            raise ValueError("not a regular file")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(checkout, directory_flags)
        opened_root = os.fstat(directory_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (
            root_info.st_dev,
            root_info.st_ino,
        ):
            raise ValueError("checkout root changed during open")
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > MAX_SOURCE_BYTES:
            raise ValueError(f"read size exceeds {MAX_SOURCE_BYTES} bytes")
        if not read:
            return b""
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError(f"read size exceeds {MAX_SOURCE_BYTES} bytes")
        return data
    except (OSError, ValueError, RuntimeError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        message = f"Code input refused for {path}: {reason}"
        logger.warning("%s", message)
        raise ValueError(message) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
