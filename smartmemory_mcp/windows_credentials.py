"""One protected Windows fallback, also usable by standalone MCP installations.

This module selects the canonical path and lock independently of wrapper imports.
The old MCP path is only a one-way migration source.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


def key_path() -> Path:
    """Canonical Windows fallback for every interpreter of the same user."""
    return (
        Path(os.environ.get("APPDATA", Path.home())) / "smartmemory/credentials/api_key"
    )


def legacy_key_path() -> Path:
    """Read-only migration source, never a standalone write destination."""
    return Path.home() / ".config/smartmemory/.api_key"


# Set only the DACL, in one operation, rather than restoring inherited defaults
# or retaining unrelated explicit ACEs. Read back the persisted ACL as SID rules.
_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$path = $env:SMARTMEMORY_CREDENTIAL_PATH
$sid = $env:SMARTMEMORY_CREDENTIAL_SID
$sections = [System.Security.AccessControl.AccessControlSections]::Access
if ([System.IO.Directory]::Exists($path)) {
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $sddl = "D:P(A;OICI;FA;;;$sid)"
} else {
    $acl = [System.Security.AccessControl.FileSecurity]::new()
    $sddl = "D:P(A;;FA;;;$sid)"
}
$acl.SetSecurityDescriptorSddlForm($sddl, $sections)
Set-Acl -LiteralPath $path -AclObject $acl
$actual = Get-Acl -LiteralPath $path
$rules = $actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
@{
    protected = $actual.AreAccessRulesProtected
    count = $rules.Count
    sid = if ($rules.Count -eq 1) { $rules[0].IdentityReference.Value } else { '' }
    inherited = if ($rules.Count -eq 1) { $rules[0].IsInherited } else { $true }
    allow = ($rules.Count -eq 1 -and $rules[0].AccessControlType -eq 'Allow')
    full_control = ($rules.Count -eq 1 -and $rules[0].FileSystemRights -eq 'FullControl')
} | ConvertTo-Json -Compress
"""


class CredentialProtectionError(OSError):
    """No new credential was persisted because protection could not be proved."""


def _current_sid() -> str:
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        sid = next(csv.reader(io.StringIO(result.stdout)))[1].strip()
        if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
            raise ValueError("Invalid current SID")
        return sid
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        IndexError,
        StopIteration,
    ) as exc:
        raise CredentialProtectionError(
            "API key not persisted: current Windows SID unavailable"
        ) from exc


def _restrict(path: Path, sid: str) -> None:
    """Replace and verify a protected current-SID-only DACL, never using /reset."""
    env = os.environ.copy()
    env.update(SMARTMEMORY_CREDENTIAL_PATH=str(path), SMARTMEMORY_CREDENTIAL_SID=sid)
    executable = (
        Path(env.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-Command", _ACL_SCRIPT],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        acl = json.loads(result.stdout)
        if not (
            acl["protected"] is True
            and acl["count"] == 1
            and acl["sid"] == sid
            and acl["inherited"] is False
            and acl["allow"] is True
            and acl["full_control"] is True
        ):
            raise ValueError("Persisted ACL is not current-SID-only")
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        raise CredentialProtectionError(
            "API key not persisted: Windows credential ACL could not be established and verified"
        ) from exc


@contextmanager
def _locked(path: Path) -> Iterator[str]:
    sid = _current_sid()
    path.parent.mkdir(parents=True, exist_ok=True)
    # No permissions are ever broadened, including when another writer holds
    # the lock. Every publication additionally protects its own empty file.
    _restrict(path.parent, sid)
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()

            def acquire():
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + 10
        while True:
            lock.seek(0)
            try:
                acquire()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CredentialProtectionError(
                        "API key not persisted: credential writer lock timed out"
                    ) from None
                time.sleep(0.05)
        try:
            yield sid
        finally:
            lock.seek(0)
            release()


def _publish(path: Path, key: str, sid: str) -> None:
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".api_key-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            # Even an existing broad explicit directory grant cannot expose a
            # secret: protection and its read-back must succeed while empty.
            _restrict(temporary, sid)
            handle.write(key.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _retire_legacy(legacy: Path | None) -> None:
    if legacy is not None:
        legacy.unlink(missing_ok=True)


def _migrate_legacy(path: Path, legacy: Path | None, sid: str) -> None:
    """Reconcile under the canonical lock, preserving a newer legacy write."""
    if legacy is None or not legacy.exists():
        return
    if not path.exists() or legacy.stat().st_mtime_ns > path.stat().st_mtime_ns:
        _restrict(legacy, sid)
        key = legacy.read_text(encoding="utf-8").strip()
        if not key:
            return
        _publish(path, key, sid)
        logger.warning(
            "Migrated legacy MCP credential to the shared protected Windows store"
        )
    # Either canonical was at least as new, or the newer legacy key was safely
    # published above. Never retire a newer write before successful migration.
    _retire_legacy(legacy)


def read_key(path: Path, *, legacy: Path | None = None) -> str:
    """Read canonical storage, migrating absent or older canonical credentials."""
    if not path.exists() and (legacy is None or not legacy.exists()):
        return ""
    with _locked(path) as sid:
        _migrate_legacy(path, legacy, sid)
        if path.exists():
            _restrict(path, sid)
            return path.read_text(encoding="utf-8").strip()
    return ""


def store_key(path: Path, key: str, *, legacy: Path | None = None) -> None:
    """Serialize updates and preserve the previous key on every ACL failure."""
    with _locked(path) as sid:
        _migrate_legacy(path, legacy, sid)
        _publish(path, key, sid)
    logger.warning(
        "OS keychain unavailable. Using the protected Windows credential file"
    )
