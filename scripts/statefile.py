#!/usr/bin/env python3
"""
statefile.py - crash-safe, multi-process-safe state file primitives.

The webhook server spawns one short-lived python process per ticket outcome
(`circuit_breaker.py record`, `decision_log.py append`), so several writers
routinely run at the same time. These helpers make that safe:

* locked(path)            exclusive fcntl.flock on a sidecar lock file; hold it
                          around any read-modify-write.
* atomic_write_bytes/text unique temp file (mkstemp) in the SAME directory ->
                          write -> fsync -> os.replace -> fsync(dir). Readers
                          see the old or the new file, never a torn one, and
                          concurrent writers can never share a temp name.
* append_line             O_APPEND + flock + one os.write per record (JSONL).

POSIX only (fcntl). Without fcntl the lock degrades to a no-op; atomic
replace and O_APPEND still apply.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator, Union

try:  # pragma: no cover - non-POSIX
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

PathLike = Union[str, os.PathLike]


class LockTimeout(Exception):
    pass


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(path: PathLike, timeout: float = 20.0) -> Iterator[None]:
    """Exclusive advisory lock on `<path>.lock` (created 0600, parent dir created 0700)."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"could not lock {lock_path} within {timeout:.0f}s")
                    time.sleep(0.02)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_bytes(path: PathLike, data: bytes, mode: int = 0o600) -> None:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(target.parent)


def atomic_write_text(path: PathLike, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def append_line(path: PathLike, line: str, mode: int = 0o600) -> None:
    """Append one record (a newline is added) with O_APPEND under an exclusive lock."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    with locked(target):
        fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
