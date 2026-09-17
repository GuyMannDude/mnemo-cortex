"""The wiki compiler applies the dreamer's second harvest gate.

Snag wiki-compile-mtime-only-harvest: after 4.21.5 the dreamer skipped
late-arriving history by the memory's own timestamp while the wiki compiler
still harvested on mtime alone, so the two nightly harvests disagreed. Both
now import ``agentb.window``; these tests pin the compiler's side.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentb import window

_WIKI_PATH = Path(__file__).resolve().parent.parent / "mnemo-wiki-compile.py"
_spec = importlib.util.spec_from_file_location("mnemo_wiki_window", _WIKI_PATH)
wiki = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wiki)

NOW = datetime(2026, 9, 12, 10, 19, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=24)


def _write(memory_dir: Path, name: str, timestamp) -> Path:
    entry = {"session_id": name, "summary": f"memory {name} with enough text to count"}
    if timestamp is not None:
        entry["timestamp"] = timestamp
    p = memory_dir / f"{name}.json"
    p.write_text(json.dumps(entry), encoding="utf-8")
    return p  # mtime = now = inside the window


def _harvest(tmp_path, monkeypatch, files: dict) -> set:
    memory_dir = tmp_path / "agents" / "bus" / "memory"
    memory_dir.mkdir(parents=True)
    for name, ts in files.items():
        _write(memory_dir, name, ts)
    monkeypatch.setattr(wiki, "AGENTB_DATA_DIR", tmp_path)
    return {m["session_id"] for m in wiki.harvest_agentb(SINCE)}


def test_compiler_uses_the_shared_predicate():
    assert wiki.predates_window is window.predates_window


def test_specimen_late_archived_april_session_is_skipped(tmp_path, monkeypatch):
    kept = _harvest(tmp_path, monkeypatch, {
        "april-bus-session": "2026-04-22T14:11:57+00:00",
        "last-night": (SINCE + timedelta(hours=3)).isoformat(),
    })
    assert kept == {"last-night"}


def test_missing_timestamp_still_harvested_by_mtime(tmp_path, monkeypatch):
    kept = _harvest(tmp_path, monkeypatch, {"no-stamp": None, "garbage": "not a date"})
    assert kept == {"no-stamp", "garbage"}


def test_just_before_cutoff_is_inside_grace(tmp_path, monkeypatch):
    ts = SINCE - window.LATE_ARRIVAL_GRACE + timedelta(minutes=1)
    assert _harvest(tmp_path, monkeypatch, {"late-flush": ts.isoformat()}) == {"late-flush"}


def test_old_mtime_still_excluded(tmp_path, monkeypatch):
    """The first gate is unchanged: a file that landed before the window is
    out even when its timestamp is fresh."""
    import os
    memory_dir = tmp_path / "agents" / "bus" / "memory"
    memory_dir.mkdir(parents=True)
    p = _write(memory_dir, "old-file", (SINCE + timedelta(hours=1)).isoformat())
    old = (SINCE - timedelta(days=2)).timestamp()
    os.utime(p, (old, old))
    monkeypatch.setattr(wiki, "AGENTB_DATA_DIR", tmp_path)
    assert wiki.harvest_agentb(SINCE) == []


def test_skip_is_counted_out_loud(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="mnemo-wiki"):
        _harvest(tmp_path, monkeypatch, {
            "a": "2026-04-22T14:11:57+00:00",
            "b": "2026-04-23T09:00:00+00:00",
        })
    assert "Skipped 2 late-arriving memories" in caplog.text
