"""Late-arriving history is not tonight's news.

The 2026-09-12 brief specimen: the dormant `bus` tenant was loaded for the
first time in months, the maintenance loop archived its April hot sessions,
and 22 fresh session_log files ABOUT April were dreamed as the night's
events. The harvest gated on file mtime only. These tests pin the second
gate: a memory whose own `timestamp` predates the window (beyond the
write-latency grace) is skipped; fresh, missing, or unparseable timestamps
keep the mtime verdict.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream_window", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)

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
    monkeypatch.setattr(dream, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(dream, "AGENTB_AGENTS", ["bus"])
    return {m["session_id"] for m in dream.harvest_agentb(SINCE)}


# ── the predicate ──

def test_april_session_predates_september_window():
    assert dream._predates_window("2026-04-22T14:11:57+00:00", SINCE)


def test_in_window_timestamp_is_kept():
    assert not dream._predates_window((SINCE + timedelta(hours=2)).isoformat(), SINCE)


def test_just_before_cutoff_is_inside_grace():
    ts = SINCE - dream.LATE_ARRIVAL_GRACE + timedelta(minutes=1)
    assert not dream._predates_window(ts.isoformat(), SINCE)


def test_beyond_grace_is_stale():
    ts = SINCE - dream.LATE_ARRIVAL_GRACE - timedelta(minutes=1)
    assert dream._predates_window(ts.isoformat(), SINCE)


def test_zulu_suffix_parses():
    assert dream._predates_window("2026-04-22T14:11:57Z", SINCE)


def test_naive_timestamp_is_read_as_utc():
    assert dream._predates_window("2026-04-22T14:11:57", SINCE)


def test_missing_or_garbage_timestamp_fails_open():
    assert not dream._predates_window(None, SINCE)
    assert not dream._predates_window("", SINCE)
    assert not dream._predates_window("last Tuesday", SINCE)


# ── the harvest ──

def test_specimen_late_archived_april_session_is_skipped(tmp_path, monkeypatch):
    kept = _harvest(tmp_path, monkeypatch, {
        "april-bus-session": "2026-04-22T14:11:57+00:00",
        "last-night": (SINCE + timedelta(hours=3)).isoformat(),
    })
    assert kept == {"last-night"}


def test_missing_timestamp_still_harvested_by_mtime(tmp_path, monkeypatch):
    kept = _harvest(tmp_path, monkeypatch, {"no-stamp": None, "garbage": "not a date"})
    assert kept == {"no-stamp", "garbage"}


def test_old_mtime_still_excluded(tmp_path, monkeypatch):
    """The first gate is unchanged: a file that landed before the window is
    out even when its timestamp is fresh."""
    import os
    memory_dir = tmp_path / "agents" / "bus" / "memory"
    memory_dir.mkdir(parents=True)
    p = _write(memory_dir, "old-file", (SINCE + timedelta(hours=1)).isoformat())
    old = (SINCE - timedelta(days=2)).timestamp()
    os.utime(p, (old, old))
    monkeypatch.setattr(dream, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(dream, "AGENTB_AGENTS", ["bus"])
    assert dream.harvest_agentb(SINCE) == []


def test_skip_is_counted_out_loud(tmp_path, monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="mnemo-dream"):
        _harvest(tmp_path, monkeypatch, {
            "a": "2026-04-22T14:11:57+00:00",
            "b": "2026-04-23T09:00:00+00:00",
        })
    assert "Skipped 2 late-arriving memories" in caplog.text
