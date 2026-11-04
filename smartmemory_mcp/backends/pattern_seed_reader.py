"""Read a MEMORY.md and its in-dir linked ``.md`` files for remote ``pattern_seed`` (SM-PATTERN-CRUD-1).

Remote mode never imports core, so this mirrors core's ``smartmemory.adherence.seed.read_memory_dir``: same link
rules, same refusal reasons, same caps, same ``(index_text, files, refused)`` shape. The service gets text only
(``POST /memory/patterns/seed``); this process is the only one that touches the client's files.
``tests/test_pattern_write_tools.py`` pins that both readers return the same thing on the same directory.

A link is read only when it is relative, has no ``..`` part, ends in ``.md`` and its real path (symlinks resolved)
stays inside the MEMORY.md directory. Every refusal logs a WARNING naming the link.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

logger = logging.getLogger(__name__)

# The same numbers as core's SEED_MAX_* and the service's request caps (pattern-contract.json "Seed.caps").
SEED_MAX_INDEX_CHARS = 256_000
SEED_MAX_FILES = 500
SEED_MAX_FILE_CHARS = 64_000
SEED_MAX_TOTAL_CHARS = 4_000_000

_LINK_LINE = re.compile(
    r"^\s*-\s+\[(?P<title>[^\]]+)\]\((?P<path>[^)]+)\)\s*[—\-:]+\s*(?P<desc>.+?)\s*$"
)


def link_refusal(link: str) -> str | None:
    """Why a link may not be read relative to the index dir, or None when it may."""
    if link.startswith(("/", "~", "\\")) or PureWindowsPath(link).drive:
        return "absolute_link"
    if ".." in PurePosixPath(link.replace("\\", "/")).parts:
        return "parent_traversal"
    return None


def read_memory_dir(
    path: str | Path,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """``(index_text, files, refused)`` for the MEMORY.md at ``path``. Raises FileNotFoundError when absent."""
    p = Path(path).expanduser().absolute()
    if not p.is_file():
        raise FileNotFoundError(f"MEMORY.md not found at {p}")
    base_dir = p.parent.resolve()
    index_text = p.read_text(encoding="utf-8")[:SEED_MAX_INDEX_CHARS]

    files: dict[str, str] = {}
    refused: dict[str, str] = {}
    total = len(index_text)
    for line in index_text.splitlines():
        m = _LINK_LINE.match(line)
        if not m:
            continue
        link = m.group("path").strip()
        if (
            link in files
            or link in refused
            or not link.endswith(".md")
            or link_refusal(link)
        ):
            continue  # the server's parser refuses absolute/.. links itself
        real = (base_dir / link).resolve()
        if not real.is_relative_to(base_dir):
            logger.warning(
                "pattern_seed: link %r resolves outside %s; not read", link, base_dir
            )
            refused[link] = "outside_dir"
            continue
        if not real.is_file():
            continue  # missing file: the bullet's description stands in
        if len(files) >= SEED_MAX_FILES:
            logger.warning(
                "pattern_seed: more than %d linked files; %r not read",
                SEED_MAX_FILES,
                link,
            )
            refused[link] = "too_many_files"
            continue
        try:
            text = real.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(
                "pattern_seed: could not read %r (%s); its description stands in",
                link,
                type(e).__name__,
            )
            continue
        if len(text) > SEED_MAX_FILE_CHARS:
            logger.warning(
                "pattern_seed: %r is over %d chars; not read", link, SEED_MAX_FILE_CHARS
            )
            refused[link] = "too_large"
            continue
        if total + len(link) + len(text) > SEED_MAX_TOTAL_CHARS:
            logger.warning(
                "pattern_seed: %r would take the request over %d chars; not read",
                link,
                SEED_MAX_TOTAL_CHARS,
            )
            refused[link] = "too_large"
            continue
        total += len(link) + len(text)
        files[link] = text
    return index_text, files, refused
