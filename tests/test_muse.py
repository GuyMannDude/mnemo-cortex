"""v4.8 Muse — the Analyst's creative sibling lens.

Contract: reads the same unprocessed Tier-2 session logs through an idea lens,
emits ONLY `idea` notes with muse provenance, keeps its own read-once marker
(muse_processed) so both lenses read every log exactly once independently,
and dry_run extracts without persisting, marking, or touching vec/embedder
(safe from a second process against a live store).
"""
from __future__ import annotations

import asyncio
import json

from agentb.analyst import muse_tenant, _parse_notes, MUSE_ALLOWED_CATEGORIES
from agentb.config import MuseConfig
from agentb.vec import VecStore

from tests.test_analyst import ScriptedReasoner, ScriptedEmbedder, _seed_log, VEC_A

IDEA_REPLY = json.dumps([
    {"category": "idea",
     "summary": "Guy connected government equipment waste to AI fraud detection — auction listings as a training signal for anomaly models.",
     "key_facts": ["government surplus auctions", "AI fraud detection", "anomaly training data"],
     "confidence": "high"},
    {"category": "idea",
     "summary": "Vague riff about music maybe",
     "key_facts": [], "confidence": "low"},
    {"category": "decision",
     "summary": "A decision the Muse must not emit even at high confidence",
     "key_facts": [], "confidence": "high"},
])


def test_muse_parse_only_accepts_idea():
    notes = _parse_notes(IDEA_REPLY, max_notes=10, allowed=MUSE_ALLOWED_CATEGORIES)
    assert len(notes) == 1
    assert notes[0]["category"] == "idea"


def test_muse_extracts_persists_and_marks_own_marker(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "Guy: you know what this reminds me of? government surplus auctions...")
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(muse_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply=IDEA_REPLY),
        ScriptedEmbedder(VEC_A), config=MuseConfig(),
    ))
    assert stats["notes_saved"] == 1

    notes = [json.loads(p.read_text()) for p in memory_dir.glob("*.json")
             if json.loads(p.read_text()).get("classified_by") == "muse"]
    assert len(notes) == 1
    note = notes[0]
    assert note["category"] == "idea"
    assert note["additional_tags"] == ["muse"]
    assert note["session_id"].startswith("muse-cc-")
    assert store.has(note["id"])

    # Muse marks its OWN marker; the Analyst's stays untouched.
    src = json.loads((memory_dir / "log1.json").read_text())
    assert src["muse_processed"] is True
    assert "analyst_processed" not in src
    store.close()


def test_lenses_read_independently(tmp_path):
    # A log the Analyst already consumed must still be fresh for the Muse.
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "riffing on chord progressions as wave interference", processed=True)
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(muse_tenant(
        "cc", memory_dir, store, ScriptedReasoner(reply=IDEA_REPLY),
        ScriptedEmbedder(VEC_A), config=MuseConfig(),
    ))
    assert stats["scanned"] == 1
    store.close()


def test_muse_dry_run_saves_and_marks_nothing(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "what if the dreamer could daydream?")

    # dry_run must not need vec or embedder at all (second-process safety).
    stats = asyncio.run(muse_tenant(
        "cc", memory_dir, None, ScriptedReasoner(reply=IDEA_REPLY),
        None, config=MuseConfig(), dry_run=True,
    ))
    assert stats["notes_extracted"] == 1
    assert [n["category"] for n in stats["notes"]] == ["idea"]
    assert stats["notes_saved"] == 0

    # Nothing persisted, nothing marked — the real pass reads it again.
    files = list(memory_dir.glob("*.json"))
    assert len(files) == 1
    src = json.loads(files[0].read_text())
    assert "muse_processed" not in src


def test_muse_llm_failure_leaves_sources_unmarked(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "an idea was voiced here")
    store = VecStore(tmp_path / "vec.sqlite")

    stats = asyncio.run(muse_tenant(
        "cc", memory_dir, store, ScriptedReasoner(error=RuntimeError("LLM down")),
        ScriptedEmbedder(VEC_A), config=MuseConfig(),
    ))
    assert stats["failed"] == 1
    src = json.loads((memory_dir / "log1.json").read_text())
    assert "muse_processed" not in src, "failed pass must be retryable"
    store.close()
