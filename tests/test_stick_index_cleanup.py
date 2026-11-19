"""The stick's sync report names the vec cleanup for memories it deleted on
this host (item 14 D, CC ruling #6864: report-only in round one).

A stick sync that deletes a memory file on this host leaves its vector in the
tenant's index. Recall already drops it (item 14 A) and /vec/backfill can
prune it (item 14 C), but nobody was told. The report now carries one line per
tenant: how many, and the exact cleanup call. The stick never opens the
VecStore (the server owns its one connection) and never runs the prune.
"""
from __future__ import annotations

from unittest.mock import patch

from tests.test_stick import courier, mem_path, write_mem, world  # noqa: F401 (fixture)

from agentb.stick import SyncReport, _index_cleanup


def _carried_deletes(world):
    """Host A and B share cc (m0-m4) and default (d0-d2); A deletes m0, m1, d0."""
    a, b, stick = world
    for i in range(5):
        write_mem(a, "cc", f"m{i}", f"cc memory {i}")
    for i in range(3):
        write_mem(a, "default", f"d{i}", f"default memory {i}")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    for tenant, mem_id in (("cc", "m0"), ("cc", "m1"), ("default", "d0")):
        mem_path(a, tenant, mem_id).unlink()
    r_a = courier(a, stick, "host-a")
    assert r_a.index_cleanup == []           # deleted on the STICK, not on host A
    return a, b, stick


def test_a_sync_that_deletes_memories_here_names_the_cleanup(world):
    a, b, stick = _carried_deletes(world)
    r = courier(b, stick, "host-b")
    assert not mem_path(b, "cc", "m0").exists()
    assert r.index_cleanup == [
        "cc: 2 memories removed here by the stick; their vectors stay in the index "
        "until POST /vec/backfill?agent_id=cc&prune_orphans=true",
        "default: 1 memory removed here by the stick; their vectors stay in the index "
        "until POST /vec/backfill?prune_orphans=true",
    ]


def test_a_dry_run_says_would_be(world):
    a, b, stick = _carried_deletes(world)
    plan = courier(b, stick, "host-b", dry_run=True)
    assert mem_path(b, "cc", "m0").exists()
    assert plan.index_cleanup[0].startswith("cc: 2 memories would be removed here by the stick")


def test_the_stick_never_opens_the_index(world):
    a, b, stick = _carried_deletes(world)
    with patch("agentb.vec.VecStore.__init__", side_effect=AssertionError("stick opened VecStore")):
        r = courier(b, stick, "host-b")
    assert len(r.index_cleanup) == 2


def test_a_sync_with_no_host_deletes_says_nothing(world):
    a, b, stick = world
    write_mem(a, "cc", "m0", "one memory")
    courier(a, stick, "host-a")
    assert courier(b, stick, "host-b").index_cleanup == []


def test_only_memory_channel_deletes_count():
    report = SyncReport(deleted_on_host=[
        "memories/cc/memory/m0.json",
        "memories/cc/trajectories/t.jsonl",     # no vec row behind a trajectory file
        "pad/notes/m1.json",                    # pad is not a memory folder
    ])
    assert _index_cleanup(report) == [
        "cc: 1 memory removed here by the stick; their vectors stay in the index "
        "until POST /vec/backfill?agent_id=cc&prune_orphans=true"]


def test_the_cli_report_prints_the_line(capsys):
    from agentb.cli import _stick_report
    report = SyncReport(deleted_on_host=["memories/cc/memory/m0.json"])
    report.index_cleanup = _index_cleanup(report)
    _stick_report(report)
    out = " ".join(capsys.readouterr().out.split())
    assert "index: cc: 1 memory removed here by the stick" in out
    assert "agent_id=cc&prune_orphans=true" in out
