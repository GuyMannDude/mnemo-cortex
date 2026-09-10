"""Shared filesystem helpers."""
import os
from pathlib import Path


def _fsync_path(p: Path) -> None:
    """Push a file's bytes to the medium. Opened read+write (no truncate)
    because Windows refuses FlushFileBuffers on a read-only handle. A
    refused OPEN (an antivirus sharing violation on a file created a
    moment ago, a read-only mount) is not a durability signal and must not
    fail a write that used to succeed; a refused FSYNC stays loud."""
    try:
        f = open(p, "r+b")
    except OSError:
        return
    try:
        os.fsync(f.fileno())
    finally:
        f.close()


def _fsync_dir(d: Path) -> None:
    """Make the rename itself durable — os.replace updates the directory
    entry, and that entry lives in the directory's own block. Not every
    platform lets you open a directory (Windows) or fsync one (some
    filesystems): those raise OSError and we accept process-crash
    durability there rather than fail the write."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """tmp + fsync + os.replace + dir fsync, so a reader can never see a
    half-written file, a crash mid-write can never destroy the previous
    contents, and once this returns the new contents survive a power cut
    (4.21.2 — before, only a process crash was covered: the S06 crash
    durability finding).

    Always UTF-8: the platform default is cp1252 on Windows, which dies on
    the first '→' in a trajectory line (and JSON/JSONL are UTF-8 by spec)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _fsync_path(tmp)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Bytes twin of atomic_write_text — same tmp + fsync + os.replace
    + dir fsync guarantee."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    _fsync_path(tmp)
    os.replace(tmp, path)
    _fsync_dir(path.parent)
