"""v4.1 Analyst — the smart-session-analysis layer.

Contract: reads unprocessed Tier-2 session logs once, extracts only
high-confidence well-formed notes, dedups against existing memories by true
cosine, persists survivors as Tier-1 with analyst provenance, and marks
sources processed even when nothing was worth keeping. An LLM failure must
leave sources UNmarked (retry next cycle), and Tier 2 is never deleted.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from agentb.analyst import analyze_tenant, _gather_candidates, _parse_notes
from agentb.config import AnalysisConfig
from agentb.vec import VecStore

VEC_A = [0.0] * 768
VEC_A[0] = 1.0
VEC_B = [0.0] * 768
VEC_B[1] = 1.0


class ScriptedReasoner:
    """Returns a fixed reply (or raises)."""
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = 0

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        self.calls += 1
        assert use_breaker is False, "analyst must not touch the live breaker"
        if self.error:
            raise self.error
        return self.reply


class ScriptedEmbedder:
    """Embeds every text to a fixed vector."""
    def __init__(self, vec):
        self.vec = vec

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        assert use_breaker is False, "analyst must not touch the live breaker"
        return list(self.vec)


def _seed_log(memory_dir: Path, mid: str, summary: str, processed=False):
    memory_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": mid, "session_id": f"cc-jsonl-{mid}", "summary": summary,
        "key_facts": [], "category": "session_log", "source": "tool",
        "created_at": time.time(), "timestamp": "2026-06-10T12:00:00",
    }
    if processed:
        entry["analyst_processed"] = True
    (memory_dir / f"{mid}.json").write_text(json.dumps(entry))


NOTE_REPLY = json.dumps([
    {"category": "decision", "summary": "Chose Hetzner over artforge self-host because a customer app must not share the box holding Mnemo and keys.",
     "key_facts": ["Hetzner CX22 Ashburn", "blast-radius isolation"], "confidence": "high"},
    {"category": "current_state", "summary": "Maybe something happened", "key_facts": [], "confidence": "low"},
    {"category": "session_log", "summary": "Raw chatter that should be rejected by category", "key_facts": [], "confidence": "high"},
])


def test_parse_notes_filters_confidence_and_category():
    notes = _parse_notes(NOTE_REPLY, max_notes=10)
    assert len(notes) == 1
    assert notes[0]["category"] == "decision"


def test_parse_notes_handles_fences_and_garbage():
    assert _parse_notes("```json\n[]\n```", 10) == []
    assert _parse_notes("I could not find anything.", 10) == []
    assert _parse_notes('{"not": "a list"}', 10) == []


def test_candidate_scan_uses_bounded_sql_index(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memory"
    store = VecStore(tmp_path / "vec.sqlite")
    for i in range(40):
        _seed_log(memory_dir, f"log{i:02d}", f"entry {i}", processed=i < 20)
        store.upsert(f"log{i:02d}", f"entry {i}", list(VEC_A),
                     source_file=(memory_dir / f"log{i:02d}.json").as_posix(),
                     created_at=float(i), category="session_log")
    real_read = Path.read_text
    reads = []
    def counted(path, *args, **kwargs):
        reads.append(path)
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counted)
    first = asyncio.run(_gather_candidates(
        memory_dir, 5, marker="analyst_processed", vec_store=store))
    assert first == []
    assert len(reads) == 20, "legacy-marker import must be capped at 4x output limit"
    reads.clear()
    candidates = asyncio.run(_gather_candidates(
        memory_dir, 5, marker="analyst_processed", vec_store=store))
    assert [entry["id"] for _, entry in candidates] == [f"log{i:02d}" for i in range(20, 25)]
    assert len(reads) == 5
    store.close()


def test_analyze_extracts_persists_and_marks(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "CC: we chose Hetzner over artforge self-host for blast radius.")
    _seed_log(memory_dir, "log2", "routine: ls, git status, nothing else")
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(analyze_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply=NOTE_REPLY),
        ScriptedEmbedder(VEC_A), config=AnalysisConfig(),
    ))
    assert stats["scanned"] == 2
    assert stats["notes_saved"] == 1

    # The note exists on disk as Tier-1 with analyst provenance
    notes = [json.loads(p.read_text()) for p in memory_dir.glob("*.json")
             if json.loads(p.read_text()).get("classified_by") == "analyst"]
    assert len(notes) == 1
    note = notes[0]
    assert note["category"] == "decision"
    assert note["source"] == "inferred"
    assert set(note["derived_from"]) == {"log1", "log2"}
    assert store.has(note["id"]), "note must be recallable via VEC"
    # v4.9.3: the note's category must reach the vec pre-filter column too —
    # omitting it left every analyst/muse note NULL there, invisible to
    # category-filtered recall and undrainable by the reclassifier.
    hit = store.search(list(VEC_A), top_k=1)[0]
    assert hit.category == "decision"

    # Sources marked processed; Tier 2 NOT deleted
    for mid in ("log1", "log2"):
        entry = json.loads((memory_dir / f"{mid}.json").read_text())
        assert entry["analyst_processed"] is True
        assert entry["category"] == "session_log"

    # Second pass: nothing left to read
    stats2 = asyncio.run(analyze_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply=NOTE_REPLY),
        ScriptedEmbedder(VEC_A), config=AnalysisConfig(),
    ))
    assert stats2["scanned"] == 0
    store.close()


def test_analyze_dedups_against_existing_memory(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "we chose Hetzner again, same reasoning")
    store = VecStore(tmp_path / "vec.sqlite")
    # The store already knows a memory at the exact same embedding.
    store.upsert("existing", "Chose Hetzner over artforge self-host.", list(VEC_A),
                 created_at=time.time())

    stats = asyncio.run(analyze_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply=NOTE_REPLY),
        ScriptedEmbedder(VEC_A), config=AnalysisConfig(),
    ))
    assert stats["notes_deduped"] == 1
    assert stats["notes_saved"] == 0
    # Source still marked processed — the knowledge exists, no retry needed.
    assert json.loads((memory_dir / "log1.json").read_text())["analyst_processed"] is True
    store.close()


def test_note_persist_failure_leaves_sources_unmarked_for_retry(tmp_path):
    """A note whose embed fails must not cost the batch its retry — otherwise
    the insight is silently lost while the source is marked read (the exact
    data-loss path the v4.1 review caught)."""
    class FailingEmbedder:
        async def embed(self, text, *, use_breaker=True, task_type="document"):
            raise RuntimeError("embedder down")

    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "we chose Hetzner for blast radius")
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(analyze_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply=NOTE_REPLY),
        FailingEmbedder(), config=AnalysisConfig(),
    ))
    assert stats["failed"] == 1
    assert stats["notes_saved"] == 0
    entry = json.loads((memory_dir / "log1.json").read_text())
    assert "analyst_processed" not in entry, "failed note persist must be retryable"
    store.close()


def test_llm_failure_leaves_sources_unmarked_for_retry(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "something important happened")
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(analyze_tenant(
        "cc", memory_dir, store, ScriptedReasoner(error=RuntimeError("LLM down")),
        ScriptedEmbedder(VEC_A), config=AnalysisConfig(),
    ))
    assert stats["failed"] == 1
    entry = json.loads((memory_dir / "log1.json").read_text())
    assert "analyst_processed" not in entry, "failed pass must be retryable"
    store.close()


def test_empty_extraction_still_marks_processed(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "ls; git status; nothing notable")
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(analyze_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply="[]"),
        ScriptedEmbedder(VEC_A), config=AnalysisConfig(),
    ))
    assert stats["notes_saved"] == 0
    assert json.loads((memory_dir / "log1.json").read_text())["analyst_processed"] is True
    store.close()


def test_deterministic_note_id_prevents_duplicates(tmp_path):
    memory_dir = tmp_path / "memory"
    store = VecStore(tmp_path / "vec.sqlite")
    cfg = AnalysisConfig(dedup_similarity=1.01)  # disable dedup gate to isolate id behavior

    _seed_log(memory_dir, "log1", "decision text")
    asyncio.run(analyze_tenant("cc", memory_dir, store,
                               ScriptedReasoner(reply=NOTE_REPLY),
                               ScriptedEmbedder(VEC_A), config=cfg))
    _seed_log(memory_dir, "log2", "same decision said again")
    asyncio.run(analyze_tenant("cc", memory_dir, store,
                               ScriptedReasoner(reply=NOTE_REPLY),
                               ScriptedEmbedder(VEC_B), config=cfg))

    analyst_notes = [p for p in memory_dir.glob("*.json")
                     if json.loads(p.read_text()).get("classified_by") == "analyst"]
    assert len(analyst_notes) == 1, "same note text must collapse to one memory"
    store.close()


def test_candidate_scan_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """The candidate scan must not run on the event loop.

    The 2026-07-26 wedge: the scan read every unprocessed log with a blocking
    `read_text` on the loop thread, so for its whole duration the server
    answered nothing at all — /health included — and the watchdog restarted a
    process that had never crashed. Slow the read down and prove a concurrent
    heartbeat still ticks.
    """
    memory_dir = tmp_path / "memory"
    for i in range(10):
        _seed_log(memory_dir, f"log{i}", "some session chatter")
    store = VecStore(tmp_path / "vec.sqlite")

    real_read_text = Path.read_text

    def slow_read_text(self, *args, **kwargs):
        time.sleep(0.02)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", slow_read_text)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        # The LLM raises, so the pass returns immediately after the scan and
        # the heartbeat is measuring the scan alone.
        stats = await analyze_tenant(
            "cc", memory_dir, store,
            ScriptedReasoner(error=RuntimeError("no LLM in this test")),
            ScriptedEmbedder(VEC_A), config=AnalysisConfig(),
        )
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        return stats, ticks

    stats, ticks = asyncio.run(scenario())
    store.close()
    assert stats["scanned"] == 10, "the scan must actually have read the logs"
    assert ticks >= 5, f"event loop starved for the whole scan ({ticks} heartbeats)"
