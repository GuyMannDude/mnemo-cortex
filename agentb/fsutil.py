"""Shared filesystem helpers."""
import contextlib
import errno
import json
import os
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, Iterator, Optional

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


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
# stripes (no growth, nothing to clean up); a record picks one by a STABLE
# hash of its canonical key (crc32: Python's hash() is salted per process),
# so two spellings of one file, and two processes, pick the same stripe. Two
# records sharing a stripe cost a wait, never a wrong answer.
#
# Cross-process (snag-mnemo-cross-process-record-writers): each stripe also
# has a lock FILE, <tenant>/.record-locks/NN.lock (beside memory/ and
# trajectories/, inside neither, so no record or channel glob sees it),
# locked with flock (POSIX) or msvcrt.locking (Windows) for the time of the
# read-change-write. The stick and the migrate CLI take the same lock, so a
# second process can no longer land between the server's read and write, or
# the server between theirs. Order is always thread stripe, then file lock.
_STRIPES = 64
_RECORD_STRIPES = tuple(threading.Lock() for _ in range(_STRIPES))
_REAL_DIRS: dict[str, str] = {}
LOCK_DIRNAME = ".record-locks"
LOCK_DEADLINE_S = 30.0


class RecordLockTimeout(OSError):
    """Another process held a record's lock file past the deadline."""


def _real_dir(parent: str) -> str:
    real = _REAL_DIRS.get(parent)
    if real is None:
        real = _REAL_DIRS.setdefault(parent, os.path.normcase(os.path.realpath(parent)))
    return real


def _record_key(path: Path) -> str:
    """The record's folder resolved once (symlinks, '..'; case folded on
    Windows only: macOS keeps case, so two casings there are two keys) plus
    its file name."""
    return os.path.join(_real_dir(str(path.parent)), os.path.normcase(path.name))


def _stripe(path: Path) -> int:
    return zlib.crc32(_record_key(Path(path)).encode("utf-8")) % _STRIPES


def record_lock(path: Path) -> threading.Lock:
    """The in-process lock guarding the memory record at `path` (the thread
    half of locked_record). Not reentrant."""
    return _RECORD_STRIPES[_stripe(path)]


def lock_file_path(path: Path) -> Path:
    """The stripe's lock file for the record at `path`: in the record's
    tenant folder (its folder's parent, resolved), never beside the record."""
    real_parent = Path(_real_dir(str(Path(path).parent)))
    return real_parent.parent / LOCK_DIRNAME / f"{_stripe(path):02d}.lock"


# One open handle per lock file per process, opened once and kept: a fresh
# open per write costs on Windows (antivirus), and on POSIX closing ANY fd
# of a file would drop an fcntl lock (we use flock, which is per open file,
# but one handle is simpler still). os.open handles are not inheritable.
# Kept, but not forever: Windows refuses to rename or remove a folder while
# ANY file under it is open (MS-FSA 2.1.4.2), so whoever closes a tenant calls
# release_lock_files on its folder (snag-mnemo-record-lock-handle-blocks-data-
# dir-move-windows). A handle a thread is using (waiting for or holding the
# lock) is never closed under it: it closes when its last user lets go.
_LOCK_FDS: dict[str, int] = {}
_LOCK_USERS: dict[str, int] = {}
_LOCK_CLOSE_WHEN_IDLE: set[str] = set()
_LOCK_FDS_GUARD = threading.Lock()


def _lock_fd(lock_path: Path) -> int:
    """The lock file's handle, counted as in use until _done_with(key)."""
    key = str(lock_path)
    with _LOCK_FDS_GUARD:
        fd = _LOCK_FDS.get(key)
        if fd is None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(key, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            _LOCK_FDS[key] = fd
        _LOCK_USERS[key] = _LOCK_USERS.get(key, 0) + 1
        return fd


def _done_with(key: str) -> None:
    with _LOCK_FDS_GUARD:
        users = _LOCK_USERS[key] - 1
        if users:
            _LOCK_USERS[key] = users
            return
        del _LOCK_USERS[key]
        if key in _LOCK_CLOSE_WHEN_IDLE:
            _LOCK_CLOSE_WHEN_IDLE.discard(key)
            os.close(_LOCK_FDS.pop(key))


def release_lock_files(folder: Path) -> None:
    """Close this process's lock-file handles under `folder` (a closed
    tenant's data dir), so Windows lets it be moved or removed. A handle in
    use now closes the moment its last user is done. A later lock under the
    folder opens a fresh handle."""
    root = os.path.join(os.path.normcase(os.path.realpath(folder)), "")
    with _LOCK_FDS_GUARD:
        for key in list(_LOCK_FDS):
            if not os.path.normcase(os.path.realpath(key)).startswith(root):
                continue
            if _LOCK_USERS.get(key):
                _LOCK_CLOSE_WHEN_IDLE.add(key)
            else:
                os.close(_LOCK_FDS.pop(key))


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            # held by another process: EACCES (or EDEADLOCK); anything else
            # (a bad handle) is a real error and stays loud
            if e.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                return False
            raise
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(lock_path: Path, deadline_s: Optional[float] = None) -> Iterator[None]:
    """Hold an exclusive OS lock on `lock_path` (created if missing).
    Non-blocking tries in a loop until the deadline, then RecordLockTimeout
    naming the file: never a silent wait forever. The OS drops the lock if
    the process dies, so a crash leaves no stale lock. Callers in one process
    must serialise on a thread lock first (locked_record does)."""
    lock_path = Path(lock_path)
    fd = _lock_fd(lock_path)
    try:
        limit = time.monotonic() + (LOCK_DEADLINE_S if deadline_s is None else deadline_s)
        pause = 0.005
        while not _try_lock(fd):
            if time.monotonic() >= limit:
                raise RecordLockTimeout(
                    f"record lock {lock_path} held by another process for more than "
                    f"{LOCK_DEADLINE_S if deadline_s is None else deadline_s:g} s")
            time.sleep(pause)
            pause = min(pause * 2, 0.1)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        _done_with(str(lock_path))


@contextlib.contextmanager
def locked_record(path: Path) -> Iterator[None]:
    """The record's full lock: its thread stripe, then its stripe's lock
    file. Not reentrant: never nest two (a second stripe while holding one
    could deadlock against another process taking them the other way).
    Blocking: call it from a worker, never on the loop."""
    with record_lock(path):
        with file_lock(lock_file_path(path)):
            yield


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
    with locked_record(path):
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
