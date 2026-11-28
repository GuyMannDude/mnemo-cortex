"""snag-mnemo-record-lock-handle-blocks-data-dir-move-windows: the record
lock keeps one open handle per lock file, and Windows refuses to move or
remove a folder while any file under it is open (MS-FSA 2.1.4.2). A closed
server (or throwaway read tenant) must hold no lock-file handle under its
data dir, and a handle a thread is using must never be closed under it.

The thread tests use a REAL second process to hold or test the OS lock,
gated by marker files (no timing luck).
"""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from agentb import fsutil
from agentb.fsutil import (
    RecordLockTimeout, file_lock, lock_file_path, locked_record, release_lock_files,
    update_record,
)
from tests.test_cross_process_lock import finish, spawn, wait_for
from tests.test_recall_drops_superseded import _client, _save

LOCK_DIR = fsutil.LOCK_DIRNAME

# Holds the record's lock until GO exists, saying HELD once it has it.
HOLDER = """
    import sys, time
    from pathlib import Path
    from agentb.fsutil import locked_record
    rec, held, go = (Path(a) for a in sys.argv[1:4])
    with locked_record(rec):
        held.touch()
        while not go.exists():
            time.sleep(0.01)
"""

# Tries the record's lock file once, briefly; prints FREE or HELD.
PROBE = """
    import sys
    from pathlib import Path
    from agentb.fsutil import RecordLockTimeout, file_lock, lock_file_path
    try:
        with file_lock(lock_file_path(Path(sys.argv[1])), deadline_s=0.3):
            print("FREE")
    except RecordLockTimeout:
        print("HELD")
"""


def _keys_under(folder: Path) -> list[str]:
    root = os.path.join(os.path.normcase(os.path.realpath(folder)), "")
    return [k for k in fsutil._LOCK_FDS if os.path.normcase(os.path.realpath(k)).startswith(root)]


def _os_handles_under(folder: Path) -> list[str]:
    """What the OS says this process has open under `folder` (Linux only)."""
    fds = Path("/proc/self/fd")
    root = os.path.realpath(folder) + os.sep
    out = []
    for fd in fds.iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(root):
            out.append(target)
    return out


def _rec(folder: Path, name: str = "m1") -> Path:
    mem = folder / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return mem / f"{name}.json"


def _probe(rec: Path) -> str:
    return finish(spawn(PROBE, str(rec))).strip()


# ── the server's close path ────────────────────────────────────────────────

def test_a_closed_server_holds_no_lock_handle_and_its_tenant_dir_moves(tmp_path):
    with _client(tmp_path) as client:
        _save(client, "the vault lives on the-north-box")
        # the save took a record lock: its handle is cached while serving
        assert _keys_under(tmp_path / "cody")
    assert _keys_under(tmp_path / "cody") == []
    if Path("/proc/self/fd").is_dir():
        assert [h for h in _os_handles_under(tmp_path / "cody") if LOCK_DIR in h] == []
    shutil.move(str(tmp_path / "cody"), str(tmp_path / "cody-moved"))   # WinError 32 before
    assert not (tmp_path / "cody").exists()


def test_a_throwaway_read_tenant_releases_its_lock_handles(tmp_path):
    from agentb.server import TenantManager
    from agentb.config import AgentBConfig
    cfg = AgentBConfig(data_dir=str(tmp_path / "data"))
    tenant, close = TenantManager(cfg).peek("nobody-yet")
    assert close is not None
    tmp = tenant["data_dir"].parent
    update_record(tenant["memory_dir"] / "x.json", lambda old: {"id": "x"})
    assert _keys_under(tmp)
    close()
    assert _keys_under(tmp) == []
    assert not tmp.exists()


# ── release_lock_files itself ──────────────────────────────────────────────

def test_release_closes_only_the_handles_under_that_folder(tmp_path):
    a, b = _rec(tmp_path / "a"), _rec(tmp_path / "b")
    update_record(a, lambda old: {"id": "a"})
    update_record(b, lambda old: {"id": "b"})
    (kb,) = _keys_under(tmp_path / "b")
    fd_b = fsutil._LOCK_FDS[kb]
    release_lock_files(tmp_path / "a")
    assert _keys_under(tmp_path / "a") == []
    assert fsutil._LOCK_FDS[kb] == fd_b
    os.fstat(fd_b)                                   # still a live handle
    # a sibling whose name starts the same is not "under" a
    c = _rec(tmp_path / "ab")
    update_record(c, lambda old: {"id": "c"})
    release_lock_files(tmp_path / "a")
    assert _keys_under(tmp_path / "ab")
    release_lock_files(tmp_path)
    assert _keys_under(tmp_path) == []


def test_after_release_the_next_lock_opens_a_fresh_handle_and_still_excludes(tmp_path):
    rec = _rec(tmp_path)
    update_record(rec, lambda old: {"id": "m1"})
    release_lock_files(tmp_path)
    with locked_record(rec):
        assert _keys_under(tmp_path)
        assert _probe(rec) == "HELD"
    assert _probe(rec) == "FREE"
    assert fsutil._LOCK_USERS == {} and fsutil._LOCK_CLOSE_WHEN_IDLE == set()


def test_a_held_lock_is_never_closed_under_its_thread(tmp_path):
    rec = _rec(tmp_path)
    inside, leave = threading.Event(), threading.Event()
    errors = []

    def holder():
        try:
            with locked_record(rec):
                inside.set()
                leave.wait(20)
        except BaseException as e:      # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=holder)
    t.start()
    assert inside.wait(20)
    (key,) = _keys_under(tmp_path)
    fd = fsutil._LOCK_FDS[key]
    release_lock_files(tmp_path)
    # still open, and the OS lock still held: a second process cannot take it
    assert fsutil._LOCK_FDS.get(key) == fd
    os.fstat(fd)
    assert _probe(rec) == "HELD"
    leave.set()
    t.join(20)
    assert not t.is_alive() and errors == []
    # its last user is done: closed now, and the lock is free
    assert _keys_under(tmp_path) == []
    assert _probe(rec) == "FREE"


def test_a_thread_waiting_for_the_lock_keeps_its_handle_through_a_release(tmp_path):
    rec = _rec(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    held, go = marks / "HELD", marks / "GO"
    other = spawn(HOLDER, str(rec), str(held), str(go))
    wait_for(held, other)
    got, errors = threading.Event(), []

    def waiter():
        try:
            with file_lock(lock_file_path(rec), deadline_s=20):
                got.set()
        except BaseException as e:      # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=waiter)
    t.start()
    # the waiter has taken the handle and is spinning on the OS lock
    deadline = 200
    while not fsutil._LOCK_USERS and deadline:
        threading.Event().wait(0.01)
        deadline -= 1
    assert fsutil._LOCK_USERS
    release_lock_files(tmp_path)
    assert _keys_under(tmp_path)                   # not closed under the waiter
    go.touch()
    finish(other)
    t.join(20)
    assert got.is_set() and errors == []
    assert _keys_under(tmp_path) == []


def test_a_lock_timeout_still_lets_go_of_the_handle(tmp_path):
    rec = _rec(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    held, go = marks / "HELD", marks / "GO"
    other = spawn(HOLDER, str(rec), str(held), str(go))
    wait_for(held, other)
    try:
        with pytest.raises(RecordLockTimeout):
            with file_lock(lock_file_path(rec), deadline_s=0.2):
                pass
        assert fsutil._LOCK_USERS == {}
        release_lock_files(tmp_path)
        assert _keys_under(tmp_path) == []        # closed at once, not deferred
    finally:
        go.touch()
        finish(other)


# ── a writer that comes AFTER the close (review of 9803a83, finding 1) ─────

def test_a_lock_taken_after_the_server_closed_lets_go_of_its_handle(tmp_path):
    with _client(tmp_path) as client:
        mid = _save(client, "the vault lives on the-north-box")
    rec = tmp_path / "cody" / "memory" / f"{mid}.json"
    assert _keys_under(tmp_path / "cody") == []
    # a worker thread still finishing (asyncio.to_thread outlives the cancel)
    update_record(rec, lambda old: {**old, "demoted_at": "late"})
    assert _keys_under(tmp_path / "cody") == []
    shutil.move(str(tmp_path / "cody"), str(tmp_path / "cody-moved"))


def test_without_for_good_a_later_lock_keeps_its_handle(tmp_path):
    # the control: what for_good changes (a throwaway tenant's folder is
    # deleted right after its release, so nothing locks there later)
    rec = _rec(tmp_path)
    update_record(rec, lambda old: {"id": "m1"})
    release_lock_files(tmp_path)
    update_record(rec, lambda old: {"id": "m1", "again": True})
    assert _keys_under(tmp_path)
    release_lock_files(tmp_path, for_good=True)
    assert _keys_under(tmp_path) == []
    update_record(rec, lambda old: {"id": "m1", "late": True})
    assert _keys_under(tmp_path) == []


# ── two users of ONE handle (finding 2) ────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="msvcrt.locking: one handle, one holder")
def test_the_handle_stays_open_until_its_last_user_is_done(tmp_path):
    # flock is per open file: a second thread on the same handle gets in at
    # once, so two direct file_lock callers can share one key
    lock = lock_file_path(_rec(tmp_path))
    in1, in2, out2, out1 = (threading.Event() for _ in range(4))

    def user(entered, leave):
        with file_lock(lock, deadline_s=5):
            entered.set()
            leave.wait(20)

    t1 = threading.Thread(target=user, args=(in1, out1))
    t2 = threading.Thread(target=user, args=(in2, out2))
    t1.start()
    assert in1.wait(20)
    t2.start()
    assert in2.wait(20)
    (key,) = _keys_under(tmp_path)
    assert fsutil._LOCK_USERS[key] == 2
    release_lock_files(tmp_path)
    out2.set()
    t2.join(20)
    assert fsutil._LOCK_USERS[key] == 1
    os.fstat(fsutil._LOCK_FDS[key])            # still open under its other user
    out1.set()
    t1.join(20)
    assert _keys_under(tmp_path) == []


# ── a data dir reached through a link (finding 3) ──────────────────────────

def test_a_data_dir_named_through_a_symlink_is_released_either_way(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    update_record(_rec(link), lambda old: {"id": "m1"})
    assert _keys_under(real)
    release_lock_files(real)                     # named by its real path
    assert _keys_under(real) == []
    update_record(_rec(real), lambda old: {"id": "m1", "x": 1})
    release_lock_files(link)                     # named by the link
    assert _keys_under(real) == []


def test_a_tenant_whose_index_close_fails_still_releases_its_lock_files(tmp_path):
    from agentb.config import AgentBConfig
    from agentb.server import TenantManager
    tm = TenantManager(AgentBConfig(data_dir=str(tmp_path / "data")))
    tenant = tm.get("cody")
    update_record(tenant["memory_dir"] / "x.json", lambda old: {"id": "x"})
    assert _keys_under(tenant["data_dir"])
    real_close = tenant["vec"].close

    def broken_close():
        raise RuntimeError("index close failed")

    tenant["vec"].close = broken_close
    try:
        with pytest.raises(RuntimeError, match="index close failed"):
            tm.close()
        assert _keys_under(tenant["data_dir"]) == []
    finally:
        real_close()
