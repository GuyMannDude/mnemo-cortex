"""snag-mnemo-cross-process-record-writers: a second PROCESS (the Cortex
Stick, `mnemo migrate reclassify`) must not lose an update the running server
makes to the same memory record or trajectory file.

Every test runs a REAL second process, gated by marker files (no timing
luck): one side reaches the dangerous point, says so, and waits for GO.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agentb.fsutil import (
    LOCK_DIRNAME, RecordLockTimeout, _LOCK_FDS, _stripe, file_lock, lock_file_path,
    locked_record, update_record,
)
from agentb.stick import init_stick, sync

REPO = Path(__file__).resolve().parents[1]


def spawn(code: str, *args: str, seed: str = "random") -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONHASHSEED": seed}
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def wait_for(path: Path, proc: subprocess.Popen | None = None, deadline: float = 20.0):
    end = time.monotonic() + deadline
    while not path.exists():
        if proc is not None and proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"process exited before {path.name}: rc={proc.returncode}\n{err}")
        if time.monotonic() > end:
            raise AssertionError(f"{path.name} never appeared")
        time.sleep(0.02)


def finish(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}\n{err}"
    return out


# A writer that reads the record, says READ, waits for GO, then writes its
# change: the slow read-change-write a stick sync or a reclassify is.
SLOW_WRITER = """
    import json, sys, time
    from pathlib import Path
    from agentb.fsutil import update_record
    rec, marks = Path(sys.argv[1]), Path(sys.argv[2])
    def mutate(old):
        (marks / "B_READ").write_text("1")
        end = time.monotonic() + 20
        while not (marks / "GO").exists():
            if time.monotonic() > end:
                raise SystemExit("no GO")
            time.sleep(0.02)
        old["category"] = "decision"
        return old
    update_record(rec, mutate)
"""

# The server's quick write (a demote), from its own process.
QUICK_WRITER = """
    import sys
    from pathlib import Path
    from agentb.fsutil import update_record
    rec, marks = Path(sys.argv[1]), Path(sys.argv[2])
    (marks / "A_TRYING").write_text("1")      # started, about to take the lock
    update_record(rec, lambda old: {**old, "demoted_at": "2026-11-27T00:00:00Z"})
    (marks / "A_DONE").write_text("1")
"""


def _record(tmp_path: Path) -> Path:
    rec = tmp_path / "data" / "agents" / "cc" / "memory" / "x.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"id": "x", "category": "session_log"}))
    return rec


def test_two_processes_never_lose_an_update(tmp_path):
    rec = _record(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    slow = spawn(SLOW_WRITER, str(rec), str(marks))
    wait_for(marks / "B_READ", slow)
    quick = spawn(QUICK_WRITER, str(rec), str(marks))
    wait_for(marks / "A_TRYING", quick)       # not "not started yet": it is trying
    time.sleep(2.0)
    quick_was_blocked = quick.poll() is None and not (marks / "A_DONE").exists()
    (marks / "GO").write_text("1")
    finish(slow)
    finish(quick)
    final = json.loads(rec.read_text())
    # the lost update, if any, shows here first
    assert final.get("category") == "decision", final
    assert final.get("demoted_at") == "2026-11-27T00:00:00Z", final
    # and it was the lock that saved it, not luck: the quick writer waited
    assert quick_was_blocked


def test_stripe_is_the_same_in_every_process(tmp_path):
    rec = _record(tmp_path)
    code = """
        import sys
        from pathlib import Path
        from agentb.fsutil import _stripe, lock_file_path
        p = Path(sys.argv[1])
        print(_stripe(p), lock_file_path(p))
    """
    outs = {finish(spawn(code, str(rec), seed=s)).strip() for s in ("1", "2", "3", "random")}
    assert outs == {f"{_stripe(rec)} {lock_file_path(rec)}"}


def test_lock_held_past_the_deadline_raises_naming_the_file(tmp_path):
    rec = _record(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    holder = spawn("""
        import sys, time
        from pathlib import Path
        from agentb.fsutil import locked_record
        rec, marks = Path(sys.argv[1]), Path(sys.argv[2])
        with locked_record(rec):
            (marks / "HELD").write_text("1")
            end = time.monotonic() + 20
            while not (marks / "GO").exists() and time.monotonic() < end:
                time.sleep(0.02)
    """, str(rec), str(marks))
    wait_for(marks / "HELD", holder)
    try:
        with pytest.raises(RecordLockTimeout) as e:
            with file_lock(lock_file_path(rec), deadline_s=0.5):
                pass
        assert str(lock_file_path(rec)) in str(e.value)
    finally:
        (marks / "GO").write_text("1")
        finish(holder)
    # control: once released, the same lock is taken at once
    with file_lock(lock_file_path(rec), deadline_s=0.5):
        pass


def test_lock_files_live_outside_every_record_and_channel_dir(tmp_path):
    rec = _record(tmp_path)
    update_record(rec, lambda old: {**old, "touched": True})
    lock = lock_file_path(rec)
    tenant = rec.parent.parent
    assert os.path.normcase(str(lock.parent)) == os.path.normcase(
        os.path.join(os.path.realpath(tenant), LOCK_DIRNAME))
    assert lock.is_file()
    assert [p.name for p in rec.parent.glob("*.json")] == ["x.json"]
    assert not (rec.parent / LOCK_DIRNAME).exists()
    assert not (tenant / "trajectories" / LOCK_DIRNAME).exists()
    # never inherited by a child process
    assert not os.get_inheritable(_LOCK_FDS[str(lock)])


def test_trajectory_append_waits_for_another_process_holding_the_file(tmp_path):
    from agentb.trajectory import TrajectoryStore
    traj_dir = tmp_path / "data" / "agents" / "cc" / "trajectories"
    store = TrajectoryStore(traj_dir)
    jsonl = traj_dir / "deploy.jsonl"
    jsonl.write_text(json.dumps({"id": "t0"}) + "\n")
    marks = tmp_path / "marks"
    marks.mkdir()
    # the stick's side: hold the file's record lock, as its rewrite does
    holder = spawn("""
        import sys, time
        from pathlib import Path
        from agentb.fsutil import locked_record
        f, marks = Path(sys.argv[1]), Path(sys.argv[2])
        with locked_record(f):
            (marks / "HELD").write_text("1")
            end = time.monotonic() + 20
            while not (marks / "GO").exists() and time.monotonic() < end:
                time.sleep(0.02)
            text = f.read_text()
            f.write_text(text)          # rewrite whole, as a pull or union does
    """, str(jsonl), str(marks))
    wait_for(marks / "HELD", holder)
    done = threading.Event()

    def save():
        store.save(agent_id="cc", task_type="deploy", task_description="ship it",
                   steps=[{"action": "a"}], outcome="ok", rating=5, embedding=[0.1] * 768)
        done.set()

    t = threading.Thread(target=save)
    t.start()
    time.sleep(1.0)
    was_blocked = not done.is_set()
    (marks / "GO").write_text("1")
    finish(holder)
    t.join(30)
    store.close()
    ids = [json.loads(line)["id"] for line in jsonl.read_text().splitlines()]
    assert len(ids) == 2 and ids[0] == "t0", ids      # the append survived the rewrite
    assert was_blocked


# ── the stick twin: a sync that scanned, then the server wrote ────────────

STICK_SYNC = """
    import json, sys, time
    from pathlib import Path
    import agentb.stick as st
    data, stick, marks, gate_dir = (Path(a) for a in sys.argv[1:5])
    mode = sys.argv[5] if len(sys.argv) > 5 else "scan"
    # Pause after the host scan of the REAL pass (sync plans with a dry run
    # of every channel first), between that scan and the writes.
    orig, orig_channel, armed = st._scan, st.sync_channel, []
    def channel(ch, *a, dry_run=False, **kw):
        if not dry_run and Path(ch.host_dir) == gate_dir:
            armed.append(1)
        return orig_channel(ch, *a, dry_run=dry_run, **kw)
    st.sync_channel = channel
    def hooked(root, pattern, transform=None):
        out = orig(root, pattern, transform)
        if Path(root) == gate_dir and armed:
            armed.clear()
            (marks / "SCANNED").write_text("1")
            end = time.monotonic() + 20
            while not (marks / "GO").exists():
                if time.monotonic() > end:
                    raise SystemExit("no GO")
                time.sleep(0.02)
        return out
    if mode == "scan":
        st._scan = hooked
    # mode "transfer": pause INSIDE the stick's lock, just before it writes a
    # host file of the gate dir (the stick's own lock, not a stand-in)
    orig_transfer = st._transfer
    def transfer(src, dst, *a, **kw):
        if mode == "transfer" and Path(dst).parent == gate_dir:
            (marks / "IN_LOCK").write_text("1")
            end = time.monotonic() + 20
            while not (marks / "GO").exists():
                if time.monotonic() > end:
                    raise SystemExit("no GO")
                time.sleep(0.02)
        return orig_transfer(src, dst, *a, **kw)
    st._transfer = transfer
    r = st.sync(data, stick, host_id="host-b", pad=False)
    print(json.dumps({"to_host": r.to_host, "to_stick": r.to_stick,
                      "deleted_on_host": r.deleted_on_host,
                      "skipped": r.skipped, "conflicts": r.conflicts}))
"""


def _mem(host: Path, mid: str) -> Path:
    return host / "agents" / "cc" / "memory" / f"{mid}.json"


def _two_hosts(tmp_path):
    a, b, mount = tmp_path / "host_a", tmp_path / "host_b", tmp_path / "usb"
    for d in (a, b, mount):
        d.mkdir()
    stick = init_stick(mount)
    m = _mem(a, "m1")
    m.parent.mkdir(parents=True)
    m.write_text(json.dumps({"id": "m1", "summary": "v1"}))
    sync(a, stick, host_id="host-a", pad=False)
    sync(b, stick, host_id="host-b", pad=False)
    assert json.loads(_mem(b, "m1").read_text())["summary"] == "v1"
    return a, b, stick


def _stick_run(tmp_path, b, stick, server_write):
    marks = tmp_path / "marks"
    marks.mkdir(exist_ok=True)
    for f in marks.iterdir():
        f.unlink()
    p = spawn(STICK_SYNC, str(b), str(stick), str(marks), str(b / "agents" / "cc" / "memory"))
    wait_for(marks / "SCANNED", p)
    if server_write:
        server_write()
    (marks / "GO").write_text("1")
    return json.loads(finish(p).strip().splitlines()[-1])


def _demote_on(host: Path):
    return lambda: update_record(_mem(host, "m1"), lambda old: {**old, "demoted_at": "D"})


def test_stick_pull_skips_a_record_the_server_changed_after_the_scan(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(a, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from A"}))
    sync(a, stick, host_id="host-a", pad=False)           # the stick now holds v2
    r = _stick_run(tmp_path, b, stick, _demote_on(b))
    on_b = json.loads(_mem(b, "m1").read_text())
    assert on_b.get("demoted_at") == "D", on_b              # the demote is still live
    assert r["skipped"] == ["memories/cc/memory/m1.json"] and r["to_host"] == []
    # base not advanced: the next sync sees a both-edit and keeps the loser
    r2 = sync(b, stick, host_id="host-b", pad=False)
    assert any("m1.json" in c for c in r2.conflicts), r2.conflicts
    saved = list((stick / "state" / "conflicts").iterdir())
    assert saved                                            # nothing is lost for good


def test_stick_pull_control_without_a_server_write_pulls(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(a, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from A"}))
    sync(a, stick, host_id="host-a", pad=False)
    r = _stick_run(tmp_path, b, stick, None)
    assert r["to_host"] == ["memories/cc/memory/m1.json"] and r["skipped"] == []
    assert json.loads(_mem(b, "m1").read_text())["summary"] == "v2 from A"


def test_stick_delete_skips_a_record_the_server_changed_after_the_scan(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(a, "m1").unlink()
    sync(a, stick, host_id="host-a", pad=False)           # the stick carries the delete
    r = _stick_run(tmp_path, b, stick, _demote_on(b))
    assert _mem(b, "m1").is_file()                          # a skipped delete never unlinks
    assert json.loads(_mem(b, "m1").read_text()).get("demoted_at") == "D"
    assert r["skipped"] == ["memories/cc/memory/m1.json"] and r["deleted_on_host"] == []


def test_stick_delete_control_without_a_server_write_deletes(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(a, "m1").unlink()
    sync(a, stick, host_id="host-a", pad=False)
    r = _stick_run(tmp_path, b, stick, None)
    assert r["deleted_on_host"] == ["memories/cc/memory/m1.json"]
    assert not _mem(b, "m1").exists()


def test_stick_both_edit_skips_when_the_server_wrote_again_after_the_scan(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(a, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from A"}))
    sync(a, stick, host_id="host-a", pad=False)
    _mem(b, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from B"}))  # both edited
    r = _stick_run(tmp_path, b, stick, _demote_on(b))
    on_b = json.loads(_mem(b, "m1").read_text())
    assert on_b.get("demoted_at") == "D" and on_b["summary"] == "v2 from B"
    assert r["skipped"] == ["memories/cc/memory/m1.json"] and r["conflicts"] == []


def test_stick_trajectory_pull_skips_when_the_server_appended_after_the_scan(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    ta = a / "agents" / "cc" / "trajectories" / "deploy.jsonl"
    ta.parent.mkdir(parents=True)
    ta.write_text(json.dumps({"id": "t-a1"}) + "\n")
    sync(a, stick, host_id="host-a", pad=False)
    sync(b, stick, host_id="host-b", pad=False)
    with ta.open("a") as f:
        f.write(json.dumps({"id": "t-a2"}) + "\n")
    sync(a, stick, host_id="host-a", pad=False)           # the stick holds t-a1, t-a2
    tb = b / "agents" / "cc" / "trajectories" / "deploy.jsonl"

    def server_append():
        with locked_record(tb):
            with tb.open("a") as f:
                f.write(json.dumps({"id": "t-b1"}) + "\n")

    marks = tmp_path / "marks"
    marks.mkdir()
    p = spawn(STICK_SYNC, str(b), str(stick), str(marks),
              str(b / "agents" / "cc" / "trajectories"))
    wait_for(marks / "SCANNED", p)
    server_append()
    (marks / "GO").write_text("1")
    r = json.loads(finish(p).strip().splitlines()[-1])
    assert r["skipped"] == ["memories/cc/trajectories/deploy.jsonl"], r
    ids = [json.loads(x)["id"] for x in tb.read_text().splitlines()]
    assert "t-b1" in ids, ids                               # the server's append survived
    sync(b, stick, host_id="host-b", pad=False)            # next sync: union, nobody loses
    ids = {json.loads(x)["id"] for x in tb.read_text().splitlines()}
    assert ids == {"t-a1", "t-a2", "t-b1"}, ids


def test_stick_push_skips_a_record_the_server_changed_after_the_scan(tmp_path):
    from agentb.stick import verify_manifest
    a, b, stick = _two_hosts(tmp_path)
    _mem(b, "m0").write_text(json.dumps({"id": "m0", "summary": "new on B"}))
    _mem(b, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from B"}))
    r = _stick_run(tmp_path, b, stick, _demote_on(b))
    assert r["skipped"] == ["memories/cc/memory/m1.json"], r
    assert r["to_stick"] == ["memories/cc/memory/m0.json"]
    verify_manifest(stick)                                  # not torn
    r2 = sync(b, stick, host_id="host-b", pad=False)       # the next sync carries it
    assert "memories/cc/memory/m1.json" in r2.to_stick
    sync(a, stick, host_id="host-a", pad=False)
    assert json.loads(_mem(a, "m1").read_text()).get("demoted_at") == "D"


def test_stick_push_control_without_a_server_write_pushes(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(b, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from B"}))
    r = _stick_run(tmp_path, b, stick, None)
    assert r["to_stick"] == ["memories/cc/memory/m1.json"] and r["skipped"] == []


def test_server_write_waits_while_the_stick_holds_the_record(tmp_path):
    a, b, stick = _two_hosts(tmp_path)
    _mem(a, "m1").write_text(json.dumps({"id": "m1", "summary": "v2 from A"}))
    sync(a, stick, host_id="host-a", pad=False)
    marks = tmp_path / "marks"
    marks.mkdir()
    p = spawn(STICK_SYNC, str(b), str(stick), str(marks),
              str(b / "agents" / "cc" / "memory"), "transfer")
    wait_for(marks / "IN_LOCK", p)                          # re-hashed, about to write
    done = threading.Event()
    t = threading.Thread(target=lambda: (_demote_on(b)(), done.set()))
    t.start()
    time.sleep(1.0)
    was_blocked = not done.is_set()
    (marks / "GO").write_text("1")
    r = json.loads(finish(p).strip().splitlines()[-1])
    t.join(30)
    assert r["to_host"] == ["memories/cc/memory/m1.json"]  # the stick wrote v2...
    on_b = json.loads(_mem(b, "m1").read_text())
    assert on_b["summary"] == "v2 from A" and on_b.get("demoted_at") == "D", on_b
    assert was_blocked                                      # ...and the demote waited for it


def test_trajectory_save_off_loop_keeps_the_loop_running(tmp_path):
    import asyncio
    from agentb.trajectory import TrajectoryStore
    traj_dir = tmp_path / "data" / "agents" / "cc" / "trajectories"
    store = TrajectoryStore(traj_dir)
    jsonl = traj_dir / "deploy.jsonl"
    jsonl.write_text(json.dumps({"id": "t0"}) + "\n")
    marks = tmp_path / "marks"
    marks.mkdir()
    holder = spawn("""
        import sys, time
        from pathlib import Path
        from agentb.fsutil import locked_record
        f, marks = Path(sys.argv[1]), Path(sys.argv[2])
        with locked_record(f):
            (marks / "HELD").write_text("1")
            end = time.monotonic() + 20
            while not (marks / "GO").exists() and time.monotonic() < end:
                time.sleep(0.02)
    """, str(jsonl), str(marks))
    wait_for(marks / "HELD", holder)

    async def main():
        ticks = 0
        task = asyncio.create_task(store.save_off_loop(
            agent_id="cc", task_type="deploy", task_description="ship it",
            steps=[{"action": "a"}], outcome="ok", rating=5, embedding=[0.1] * 768))
        for _ in range(20):                                # 1 s of loop time
            await asyncio.sleep(0.05)
            ticks += 1
        pending = not task.done()
        (marks / "GO").write_text("1")
        await asyncio.wait_for(task, 30)
        return ticks, pending

    ticks, pending = asyncio.run(main())
    finish(holder)
    store.close()
    assert pending and ticks == 20                          # waited, and the loop ran
    assert len(jsonl.read_text().splitlines()) == 2
