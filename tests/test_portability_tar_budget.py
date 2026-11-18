"""SVC-OKF-IMPORT-TAR-HEADER-BOMB-1 (c): local OKF extraction bounds the decoded tar stream, twin of the service.

stdlib reads each PAX/GNU extension header whole before it returns a member, so a ~130 KB archive with a 128 MiB PAX
comment made the process hold 512 MiB. Each hostile archive is built at runtime and must be refused from its header
with bounded peak memory; a bundle written the way tarfile writes it still extracts. The parity row pins the budget
to the values in smart-memory-service ``memory_service/api/routes/portability.py`` (this package cannot import it).
"""

from __future__ import annotations

import gzip
import io
import tarfile
import tracemalloc
from pathlib import Path

import pytest

from smartmemory_mcp.tools import portability_tools

MIB = 1024 * 1024
HEADER_BOMB_BYTES = 128 * MIB
PEAK_BOUND = (
    4 * MIB
)  # base held the whole header (512 MiB for 128 MiB); head stops at the 512-byte header
ROOT = "smartmemory-okf"
INDEX = b'---\nokf_version: "0.1"\n---\n'
PAGES = {
    "a.md": b"---\ntype: semantic\n---\n\nalpha\n",
    "b.md": b"---\ntype: semantic\n---\n\nbeta\n",
}


class _RecordingBudget(portability_tools._DecodedBudget):
    made: list[_RecordingBudget] = []

    def __init__(self) -> None:
        super().__init__()
        self.made.append(self)


def _extract(monkeypatch, archive: Path, destination: Path):
    """Run the real extractor, returning (error or bundle dir, peak traced bytes, furthest any pass decoded)."""
    _RecordingBudget.made = []
    monkeypatch.setattr(portability_tools, "_DecodedBudget", _RecordingBudget)
    tracemalloc.start()
    try:
        result = portability_tools._extract_bundle(archive, destination)
    except (ValueError, tarfile.TarError) as exc:
        result = exc
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return (
        result,
        peak,
        max((budget.decoded for budget in _RecordingBudget.made), default=0),
    )


def _bundle_tar() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, data in {"index.md": INDEX, **PAGES}.items():
            info = tarfile.TarInfo(f"{ROOT}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _extension(kind: bytes, body: bytes) -> bytes:
    header = tarfile.TarInfo("././@PaxHeader")
    header.type = kind
    header.size = len(body)
    return (
        header.tobuf(format=tarfile.USTAR_FORMAT)
        + body
        + b"\0" * (-len(body) % tarfile.BLOCKSIZE)
    )


def _member(name: str, data: bytes) -> bytes:
    """One regular member (header block, data, padding), with no end-of-archive blocks."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return (
        info.tobuf(format=tarfile.USTAR_FORMAT)
        + data
        + b"\0" * (-len(data) % tarfile.BLOCKSIZE)
    )


def _pax_record(keyword: str, value: str) -> bytes:
    payload = f" {keyword}={value}\n".encode()
    length = len(payload) + len(str(len(payload)))
    if len(str(length)) != len(str(len(payload))):
        length += 1
    return str(length).encode() + payload


def _write_header_bomb(path: Path, kind: bytes, body_bytes: int) -> None:
    """A gzip tar whose first member is a ``kind`` extension with a ``body_bytes`` body, written 1 MiB at a time."""
    if kind == tarfile.XHDTYPE:
        prefix = b" comment="
        size = len(prefix) + body_bytes + 1
        size += len(str(size + len(str(size))))
        head, tail = str(size).encode() + prefix, b"\n"
    else:
        head, tail = f"{ROOT}/".encode(), b".md\0"
        size = len(head) + body_bytes + len(tail)
    header = tarfile.TarInfo(
        "././@PaxHeader" if kind == tarfile.XHDTYPE else "././@LongLink"
    )
    header.type = kind
    header.size = size
    chunk = b"A" * MIB
    with gzip.open(path, "wb", compresslevel=9) as out:
        out.write(
            header.tobuf(
                format=tarfile.USTAR_FORMAT
                if kind == tarfile.XHDTYPE
                else tarfile.GNU_FORMAT
            )
        )
        out.write(head)
        for _ in range(body_bytes // MIB):
            out.write(chunk)
        out.write(chunk[: body_bytes % MIB])
        out.write(tail + b"\0" * (-size % tarfile.BLOCKSIZE))
        out.write(_bundle_tar())


def test_pax_comment_header_bomb_is_refused_with_bounded_memory(monkeypatch, tmp_path):
    archive = tmp_path / "pax-bomb.tar.gz"
    _write_header_bomb(archive, tarfile.XHDTYPE, HEADER_BOMB_BYTES)
    assert archive.stat().st_size < 256 * 1024

    result, peak, decoded = _extract(monkeypatch, archive, tmp_path / "out")

    assert isinstance(result, portability_tools.ArchiveBudgetError)
    assert str(result) == "OKF archive extension header exceeds its size limit"
    assert peak < PEAK_BOUND, f"peak {peak} bytes"
    assert decoded <= portability_tools._DECODED_SLACK_BYTES
    assert not any((tmp_path / "out").iterdir())


def test_gnu_long_name_header_bomb_gets_the_same_refusal(monkeypatch, tmp_path):
    archive = tmp_path / "gnu-bomb.tar.gz"
    _write_header_bomb(archive, tarfile.GNUTYPE_LONGNAME, HEADER_BOMB_BYTES)

    result, peak, decoded = _extract(monkeypatch, archive, tmp_path / "out")

    assert str(result) == "OKF archive extension header exceeds its size limit"
    assert peak < PEAK_BOUND, f"peak {peak} bytes"
    assert decoded <= portability_tools._DECODED_SLACK_BYTES


def test_decoded_stream_over_budget_with_many_small_members_is_refused(
    monkeypatch, tmp_path
):
    """2,000 small unknown-type members (128 MiB of skipped data). Base read every header (and so decoded all of
    it) before validating; head validates each member as it streams, and refuses the first one."""
    archive = tmp_path / "many-small.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=1) as out:
        filler = b"\0" * (64 * 1024)
        for n in range(2_000):
            info = tarfile.TarInfo(f"{ROOT}/skip/{n}")
            info.type = b"Z"
            info.size = len(filler)
            out.addfile(info, io.BytesIO(filler))

    result, peak, decoded = _extract(monkeypatch, archive, tmp_path / "out")

    assert str(result) == f"Unsupported archive member type: {ROOT}/skip/0"
    assert peak < PEAK_BOUND, f"peak {peak} bytes"
    assert decoded < portability_tools._DECODED_SLACK_BYTES + 2 * MIB


def test_extension_chains_global_headers_and_sparse_members_are_refused(
    monkeypatch, tmp_path
):
    small = _pax_record("comment", "x" * 32)
    sparse = tarfile.TarInfo(f"{ROOT}/sparse.md")
    sparse.type = tarfile.GNUTYPE_SPARSE
    cases = {
        "chain": (
            b"".join(_extension(tarfile.XHDTYPE, small) for _ in range(5)),
            "chains too many extension headers",
        ),
        "global": (
            b"".join(
                _extension(tarfile.XGLTYPE, small) + _member(f"{ROOT}/g{n}.md", b"g")
                for n in range(9)
            ),
            "too many global extension headers",
        ),
        "sparse-1.0": (
            _extension(
                tarfile.XHDTYPE,
                _pax_record("GNU.sparse.major", "1")
                + _pax_record("GNU.sparse.minor", "0"),
            ),
            "cannot contain sparse members",
        ),
        "gnu-sparse": (
            sparse.tobuf(format=tarfile.GNU_FORMAT),
            "cannot contain sparse members",
        ),
    }
    for name, (prefix, text) in cases.items():
        archive = tmp_path / f"{name}.tar.gz"
        archive.write_bytes(gzip.compress(prefix + _bundle_tar()))

        result, _peak, _decoded = _extract(
            monkeypatch, archive, tmp_path / f"out-{name}"
        )

        assert isinstance(result, portability_tools.ArchiveBudgetError), name
        assert text in str(result), name


def test_normal_bundle_with_pax_headers_still_extracts(monkeypatch, tmp_path):
    """Written by tarfile.add from disk: a PAX header per member on 3.13 and a long page name on every version."""
    source = tmp_path / "src" / ROOT
    long_page = "c" * 140 + ".md"
    for name, data in {
        "index.md": INDEX,
        **PAGES,
        long_page: b"---\ntype: semantic\n---\n\ngamma\n",
    }.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(data)
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        out.add(source, arcname=ROOT)

    bundle_dir, _peak, _decoded = _extract(monkeypatch, archive, tmp_path / "out")

    assert bundle_dir == tmp_path / "out" / ROOT
    assert (bundle_dir / long_page).read_bytes().endswith(b"gamma\n")
    assert portability_tools._bundle_page_count(bundle_dir) == 3


def test_budget_parity_with_the_service_extractor():
    """Same budget as smart-memory-service portability.py (SVC-OKF-IMPORT-TAR-HEADER-BOMB-1); change both together."""
    assert {
        "slack": portability_tools._DECODED_SLACK_BYTES,
        "extension_header": portability_tools._MAX_EXTENSION_HEADER_BYTES,
        "extension_total": portability_tools._MAX_EXTENSION_TOTAL_BYTES,
        "chain": portability_tools._MAX_EXTENSION_CHAIN,
        "global": portability_tools._MAX_GLOBAL_HEADERS,
    } == {
        "slack": 1024 * 1024,
        "extension_header": 64 * 1024,
        "extension_total": 64 * 1024 * 1024,
        "chain": 4,
        "global": 8,
    }
    assert portability_tools._EXTENSION_TYPES == {b"L", b"K", b"x", b"g", b"X"}


@pytest.mark.parametrize("cut", [10, 600])
def test_truncated_gzip_is_a_read_error(tmp_path, cut):
    archive = tmp_path / "cut.tar.gz"
    archive.write_bytes(gzip.compress(_bundle_tar())[:-cut])
    with pytest.raises(tarfile.ReadError):
        portability_tools._extract_bundle(archive, tmp_path / "out")
