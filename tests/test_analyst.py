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

from agentb.analyst import analyze_tenant, _parse_notes
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
