"""v4.9.16 — atomic in-place memory writers (cross-version review findings).

The v4.9.4→v4.9.15 sprint made writeback and L2 saves atomic, but left the
analyst note-create, analyst mark-processed, and classify reclassify writers
on plain write_text. Since v4.9.14 the l3_scan disk walk reads those same
files from a worker thread, so a plain truncate-then-write is a torn-read
window — and a crash mid-write destroys an existing memory outright.

Contract under test:
  - atomic_write_text never exposes a half-written file and never destroys
    the previous contents on a failed write.
  - analyst (note create + mark processed) and classify (reclassify) go
    through atomic_write_text.
  - L2Index.add never evicts more than one entry per add — a legacy over-cap
    index must not be truncated on first touch.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from agentb.analyst import analyze_tenant
from agentb.cache import L2Index
from agentb.classify import reclassify_memory_dir
from agentb.config import AnalysisConfig, CacheConfig
from agentb.fsutil import atomic_write_text
from agentb.vec import VecStore

VEC = [0.0] * 768
VEC[0] = 1.0


class ScriptedReasoner:
    def __init__(self, reply):
        self.reply = reply

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return self.reply


class ScriptedEmbedder:
    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)


def _seed_log(memory_dir: Path, mid: str, summary: str):
    memory_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": mid, "session_id": f"cc-jsonl-{mid}", "summary": summary,
        "key_facts": [], "category": "session_log", "source": "tool",
        "created_at": time.time(), "timestamp": "2026-07-07T12:00:00",
    }
    (memory_dir / f"{mid}.json").write_text(json.dumps(entry))


# ── atomic_write_text contract ──

def test_atomic_write_text_writes_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "mem.json"
    atomic_write_text(target, '{"ok": true}')
    assert json.loads(target.read_text()) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_failure_preserves_previous_contents(tmp_path, monkeypatch):
    """The F2 scenario: a crash mid-write must not destroy the old memory.
    Plain write_text truncates first, so a failure there loses everything;
    tmp+replace must leave the original byte-identical."""
    target = tmp_path / "mem.json"
    target.write_text('{"summary": "irreplaceable"}')

    real_write_text = Path.write_text

    def exploding_write_text(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("disk full mid-write")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", exploding_write_text)
    with pytest.raises(OSError):
        atomic_write_text(target, '{"summary": "half-writ')
    monkeypatch.undo()

    assert json.loads(target.read_text()) == {"summary": "irreplaceable"}


# ── the three writer sites go through atomic_write_text ──

NOTE_REPLY = json.dumps([
    {"category": "decision", "summary": "A real decision worth keeping around for the note-create path.",
     "key_facts": ["fact"], "confidence": "high"},
])


def test_analyst_writers_are_atomic(tmp_path, monkeypatch):
    """Both analyst writes — the new note and the mark-processed rewrite —
    must route through atomic_write_text (torn-read window vs the off-loop
    l3_scan walker since v4.9.14)."""
    memory_dir = tmp_path / "memories"
    _seed_log(memory_dir, "log1", "We decided something important today.")
    vec = VecStore(tmp_path / "vec.sqlite")

    written: list[Path] = []
    import agentb.analyst as analyst_mod
    real_atomic = analyst_mod.atomic_write_text

    def spying_atomic(path, text):
        written.append(Path(path))
        return real_atomic(path, text)

    monkeypatch.setattr(analyst_mod, "atomic_write_text", spying_atomic)

    stats = asyncio.run(analyze_tenant(
        "cc", memory_dir, vec, ScriptedReasoner(NOTE_REPLY), ScriptedEmbedder(),
        config=AnalysisConfig(),
    ))
    assert stats["notes_saved"] == 1

    names = [p.name for p in written]
    # exactly two atomic writes: the new note + the mark-processed rewrite
    # of the source log — and both files exist on disk afterwards
    assert "log1.json" in names, names
    note_writes = [n for n in names if n != "log1.json"]
    assert len(note_writes) == 1, names
    assert (memory_dir / note_writes[0]).is_file()
    # and the writes actually landed as valid JSON
    marked = json.loads((memory_dir / "log1.json").read_text())
    assert marked["analyst_processed"] is True


def test_classify_reclassify_write_is_atomic(tmp_path, monkeypatch):
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir(parents=True)
    entry = {
        "id": "m1", "summary": "Rocky's Switch runs on port 50060 on IGOR.",
        "key_facts": [], "category": "unknown", "source": "tool",
        "created_at": time.time(),
    }
    (memory_dir / "m1.json").write_text(json.dumps(entry))

    written: list[Path] = []
    import agentb.classify as classify_mod
    real_atomic = classify_mod.atomic_write_text

    def spying_atomic(path, text):
        written.append(Path(path))
        return real_atomic(path, text)

    monkeypatch.setattr(classify_mod, "atomic_write_text", spying_atomic)

    stats = asyncio.run(reclassify_memory_dir(
        memory_dir, ScriptedReasoner("topology"), use_breaker=False,
    ))
    assert stats["reclassified"] == 1
    assert [p.name for p in written] == ["m1.json"]
    assert json.loads((memory_dir / "m1.json").read_text())["category"] == "topology"


# ── F3: legacy over-cap L2 index must not be truncated on first add ──

def _l2_entry(i: int) -> dict:
    return {"id": f"legacy{i}", "content": f"c{i}", "source": "s",
            "embedding": [0.1], "metadata": {}, "created_at": float(i)}


def test_l2_add_never_mass_evicts_legacy_entries(tmp_path):
    index_dir = tmp_path / "l2"
    index_dir.mkdir()
    legacy = [_l2_entry(i) for i in range(10)]
    (index_dir / "index.json").write_text(json.dumps(legacy))

    l2 = L2Index(index_dir, CacheConfig(l2_max_entries=5))
    assert l2.size == 10

    asyncio.run(l2.add("new content", "test", [0.2]))
    # 11 entries, cap 5: eviction must be SKIPPED, not drop 6 legacy entries
    assert l2.size == 11
    on_disk = json.loads((index_dir / "index.json").read_text())
    assert len(on_disk) == 11


def test_l2_add_still_evicts_one_at_the_cap(tmp_path):
    index_dir = tmp_path / "l2"
    index_dir.mkdir()
    at_cap = [_l2_entry(i) for i in range(5)]
    (index_dir / "index.json").write_text(json.dumps(at_cap))

    l2 = L2Index(index_dir, CacheConfig(l2_max_entries=5))
    asyncio.run(l2.add("new content", "test", [0.2]))
    # normal steady-state: oldest single entry evicted, cap held
    assert l2.size == 5
    ids = [e["id"] for e in l2.entries]
    assert "legacy0" not in ids
