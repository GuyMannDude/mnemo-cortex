"""M-group storage-integrity regression tests (clean-room review, S126).

- facts_store: BEGIN IMMEDIATE makes save()'s read-check-write atomic across
  writers — concurrent saves used to race into an uncaught IntegrityError.
- L2Index: atomic _save (crash mid-write used to wipe the index) + the new
  l2_max_entries cap (the index was unbounded).
- migrate._sqlite_snapshot: WAL-safe backup — shutil.copy2 missed
  uncheckpointed -wal pages.
- classify: re-reads the memory JSON before writing so seconds of LLM latency
  can't clobber a concurrent writer's fields.
- redact: the credential patterns the old alternation missed (bare *_TOKEN /
  SECRET, PRIVATE_KEY assignments, URL-embedded passwords).
"""
import asyncio
import json
import sqlite3
import threading

import pytest

from agentb.cache import L2Index
from agentb.classify import reclassify_memory_dir
from agentb.config import CacheConfig
from agentb.facts_store import FactsStore
from agentb.migrate import _sqlite_snapshot
from agentb.redact import redact_text

VEC = [0.1] * 768


# ── facts_store: cross-writer atomicity ──

def test_concurrent_first_saves_never_raise(tmp_path):
    """Two writers racing the same brand-new (entity, attribute) used to both
    read 'no existing fact' and collide on INSERT (IntegrityError → 500).
    BEGIN IMMEDIATE serializes them; both calls must succeed."""
    path = tmp_path / "facts.sqlite"
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer(value: str):
        store = FactsStore(path)
        barrier.wait()
        for i in range(20):
            try:
                store.save(entity=f"e{i}", attribute="a", value=value,
                           confidence="high_probability",
                           evidence_source="race test")
            except Exception as exc:  # pragma: no cover - failure evidence
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(v,)) for v in ("one", "two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent saves raised: {errors!r}"
    store = FactsStore(path)
    for i in range(20):
        fact = store.get(f"e{i}", "a")
        assert fact is not None
        assert fact.value in ("one", "two")


def test_save_and_demote_still_work_single_writer(tmp_path):
    store = FactsStore(tmp_path / "facts.sqlite")
    r = store.save(entity="igor", attribute="os", value="ubuntu",
                   confidence="verified", evidence_source="test")
    assert r.written
    d = store.demote("igor", "os", reason="test demote")
    assert d.written
    assert store.get("igor", "os", include_false=True).confidence == "false"


# ── L2Index: atomic save + cap ──

def test_l2_save_is_atomic_no_tmp_left_behind(tmp_path):
    l2 = L2Index(tmp_path / "l2", CacheConfig())
    asyncio.run(l2.add("some content", "test", list(VEC)))
    index_file = tmp_path / "l2" / "index.json"
    assert index_file.exists()
    assert not (tmp_path / "l2" / "index.json.tmp").exists()
    assert json.loads(index_file.read_text())  # valid JSON, one entry


def test_l2_cap_evicts_oldest_first(tmp_path):
    cfg = CacheConfig(l2_max_entries=3)
    l2 = L2Index(tmp_path / "l2", cfg)
    ids = [asyncio.run(l2.add(f"content {i}", "test", list(VEC)))
           for i in range(5)]
    assert l2.size == 3
    kept = {e["id"] for e in l2.entries}
    assert kept == set(ids[2:]), "eviction must drop the oldest entries"
    # And the cap survives a reload from disk.
    l2b = L2Index(tmp_path / "l2", cfg)
    assert l2b.size == 3


def test_l2_cap_zero_disables(tmp_path):
    l2 = L2Index(tmp_path / "l2", CacheConfig(l2_max_entries=0))
    for i in range(4):
        asyncio.run(l2.add(f"content {i}", "test", list(VEC)))
    assert l2.size == 4


# ── migrate: WAL-safe sqlite snapshot ──

def test_sqlite_snapshot_captures_uncheckpointed_wal_pages(tmp_path):
    src = tmp_path / "live.sqlite"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('committed-but-in-wal')")
    conn.commit()
    # Connection stays OPEN: the row lives in live.sqlite-wal, not the main
    # file — exactly the state a live-server backup sees. A shutil.copy2 of
    # just the main file would miss it.
    try:
        dst = tmp_path / "snapshot.sqlite"
        _sqlite_snapshot(src, dst)
        rows = sqlite3.connect(dst).execute("SELECT x FROM t").fetchall()
        assert rows == [("committed-but-in-wal",)]
    finally:
        conn.close()


# ── classify: re-read before write ──

def test_reclassify_preserves_concurrent_marker(tmp_path, monkeypatch):
    """A field written by a concurrent writer between classify's read and its
    write (LLM latency window) must survive the reclassify write."""
    mem = tmp_path / "memory"
    mem.mkdir()
    path = mem / "m1.json"
    path.write_text(json.dumps({
        "id": "m1", "summary": "some session activity", "key_facts": [],
        "category": "unknown", "created_at": 1.0,
    }))

    async def classify_and_mutate(reasoner, summary, key_facts, **kw):
        # Simulate the Analyst marking the file mid-LLM-call.
        entry = json.loads(path.read_text())
        entry["analyst_processed"] = True
        path.write_text(json.dumps(entry))
        return "decision", "llm"

    monkeypatch.setattr("agentb.classify.classify_category", classify_and_mutate)

    stats = asyncio.run(reclassify_memory_dir(mem, reasoner=None))
    assert stats["reclassified"] == 1

    final = json.loads(path.read_text())
    assert final["category"] == "decision", "reclassify result must land"
    assert final.get("analyst_processed") is True, (
        "concurrent writer's field was clobbered by a stale write")


# ── redact: broadened credential patterns ──

@pytest.mark.parametrize("text", [
    'DISCORD_TOKEN=abcdef1234567890XYZpq',
    'SECRET: abcdef1234567890XYZ',
    'PRIVATE_KEY="abcdef1234567890XYZ"',
    'export MY_PASSPHRASE=abcdef1234567890XYZ',
    'postgres://mnemo:sup3rs3cret@db.internal:5432/cortex',
    'redis://user:hunter22@cache.local',
])
def test_redact_catches_new_credential_forms(text):
    clean, counts = redact_text(text)
    assert sum(counts.values()) == 1, f"not redacted: {text!r} -> {clean!r}"
    assert "[REDACTED:" in clean


def test_redact_url_credential_keeps_username_and_host():
    clean, counts = redact_text("postgres://mnemo:sup3rs3cret@db.internal/cortex")
    assert counts.get("url-credential") == 1
    assert "mnemo" in clean and "db.internal" in clean
    assert "sup3rs3cret" not in clean


def test_redact_new_patterns_are_idempotent():
    text = "DISCORD_TOKEN=abcdef1234567890XYZ and postgres://u:p4ssw0rd@h/db"
    once, counts1 = redact_text(text)
    twice, counts2 = redact_text(once)
    assert twice == once
    assert not counts2


def test_redact_still_ignores_placeholders():
    clean, counts = redact_text("DISCORD_TOKEN=${DISCORD_TOKEN}")
    assert not counts
    assert clean == "DISCORD_TOKEN=${DISCORD_TOKEN}"
