"""Shared filesystem helpers."""
import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional


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


# ── Item 17 (b): one lock per memory record, inside this process ──
# Every writer of a memory/<id>.json record (writeback, Analyst/Muse notes,
# archived sessions, demote, supersede marks, lens markers, reclassify, the
# proposal applier) reads, changes and writes it under the record's lock, so
# no writer can land between another's read and write. A FIXED set of 64
# locks (no growth, nothing to clean up); a record picks one by its canonical
# key, so two spellings of one file share a lock. Two records sharing a
# stripe cost a wait, never a wrong answer. Another PROCESS (the stick, the
# migrate CLI) is not covered: snag-mnemo-cross-process-record-writers.
_RECORD_STRIPES = tuple(threading.Lock() for _ in range(64))
_REAL_DIRS: dict[str, str] = {}


def _record_key(path: Path) -> str:
    """The record's folder resolved once (symlinks, '..'; case folded on
    Windows only: macOS keeps case, so two casings there are two keys) plus
    its file name."""
    parent = str(path.parent)
    real = _REAL_DIRS.get(parent)
    if real is None:
        real = _REAL_DIRS.setdefault(parent, os.path.normcase(os.path.realpath(parent)))
    return os.path.join(real, os.path.normcase(path.name))


def record_lock(path: Path) -> threading.Lock:
    """The lock guarding the memory record at `path`. Not reentrant."""
    return _RECORD_STRIPES[hash(_record_key(Path(path))) % len(_RECORD_STRIPES)]


def update_record(path: Path, mutate: Callable[[Optional[dict]], Optional[dict]],
                  ) -> tuple[Optional[dict], Optional[dict]]:
    """Read, change and write one memory record under its lock.

    mutate(old) gets the parsed record (None when the file does not exist)
    and returns the dict to write, or None to write nothing. An unreadable
    record raises, before mutate runs. Returns (old, new).

    mutate MUST BE PURE: no I/O, no await, no database, no second
    update_record (the lock is not reentrant). The proposal applier holds
    the facts transaction while it takes a record lock, so nothing may hold
    a record lock and then ask for the facts database. It may raise; the
    lock is released and nothing is written.

    Blocking: call it from a worker (asyncio.to_thread), never on the loop."""
    path = Path(path)
    with record_lock(path):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            old = None
        else:
            old = json.loads(text)
            if not isinstance(old, dict):
                raise ValueError(f"memory record {path} is not a JSON object")
        new = mutate(None if old is None else json.loads(text))
        if new is not None:
            atomic_write_text(path, json.dumps(new, indent=2, default=str))
        return old, new
