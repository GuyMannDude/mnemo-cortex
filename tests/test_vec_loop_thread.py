"""Item 17 (d): the shared VecStore connection is used on the event loop only.

The lens candidate scan used to read the index AND write lens_processed
(`with self._conn:` = commit, or rollback on error) inside asyncio.to_thread,
on the tenant's one shared connection. A commit there can commit half of a
loop-side delete_many; a rollback can undo a loop-side upsert. Now the index
calls run on the loop and only the file reads go to the worker.
"""
from __future__ import annotations

import asyncio
import threading

from agentb.analyst import analyze_tenant
from agentb.config import AnalysisConfig
from agentb.vec import VecStore

from tests.test_analyst import NOTE_REPLY, VEC_A, VEC_B, ScriptedEmbedder, ScriptedReasoner, _seed_log


def _index_log(store, memory_dir, mid, created_at):
    store.upsert(mid, f"entry {mid}", list(VEC_A),
                 source_file=(memory_dir / f"{mid}.json").as_posix(),
                 created_at=created_at, category="session_log")


def test_the_lens_pass_uses_the_shared_connection_on_the_loop_only(tmp_path, vec_thread_tripwire):
    memory_dir = tmp_path / "memory"
    # "old" carries the marker in its JSON but not in lens_processed: the scan
    # must mark it (the write that used to run in the worker). "new" is a
    # real candidate, so the whole pass runs: scan, note, persist, mark.
    _seed_log(memory_dir, "old", "already read by an older analyst", processed=True)
    _seed_log(memory_dir, "new", "CC: we chose Hetzner over artforge self-host for blast radius.")
    store = VecStore(tmp_path / "vec.sqlite")
    _index_log(store, memory_dir, "old", 1.0)
    _index_log(store, memory_dir, "new", 2.0)
    calls = vec_thread_tripwire(store)

    async def run():
        loop_thread = threading.get_ident()
        stats = await analyze_tenant(
            "cc", memory_dir, store, ScriptedReasoner(reply=NOTE_REPLY),
            # VEC_B: unlike the indexed logs, so the dedup gate lets the note save
            ScriptedEmbedder(VEC_B), config=AnalysisConfig())
        return loop_thread, stats

    loop_thread, stats = asyncio.run(run())
    assert stats["scanned"] == 1 and stats["notes_saved"] == 1
    assert calls, "the tripwire saw nothing: it is not wired to the store"
    off_loop = [op for ident, op in calls if ident != loop_thread]
    assert off_loop == [], f"shared VecStore connection used off the loop: {off_loop}"
    # The legacy-marked log was marked in the index (on the loop): it is no
    # longer pending, and neither is the one this pass read.
    assert store.pending_lens_sources("analyst_processed", 10) == []
    store.close()


def test_the_tripwire_fires_on_a_worker_thread(tmp_path, vec_thread_tripwire):
    """Control: prove the tripwire CAN fail (a call from to_thread is seen
    as off the loop), so a green run above means something."""
    store = VecStore(tmp_path / "vec.sqlite")
    calls = vec_thread_tripwire(store)

    async def run():
        loop_thread = threading.get_ident()
        await asyncio.to_thread(store.mark_lens_processed, "m1", "analyst_processed")
        return loop_thread

    loop_thread = asyncio.run(run())
    assert {op for ident, op in calls if ident != loop_thread} >= {"enter", "execute", "exit"}
    store.close()
