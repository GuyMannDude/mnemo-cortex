"""The dreamer saved every dream twice into its own tenant (#6795): a direct
<dream_id>.json in agents/dreamer/memory (never indexed) and the /writeback
save (indexed). Any walker of that folder (vec backfill, the full reindex
in agentb/migrate.py) would index each dream a second time. On one live host,
all 127 unindexed dreamer records were such twins.

Now the dreamer keeps its own record under dreams/records/, outside every
memory folder; get_last_dream_time reads both places; and a one-time
`--move-direct-copies [--apply]` moves the old twins out, never deleting.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentb.vec import iter_memory_entries

_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream_records", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)


class _Resp:
    status_code = 200

    def json(self):
        return {"memory_id": "server-id"}


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    memory = tmp_path / "agents" / "dreamer" / "memory"
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path / "dreams")
    monkeypatch.setattr(dream, "DREAMER_MEMORY_DIR", memory)
    monkeypatch.setenv("MNEMO_RULEKEEPER_ADVISORY", str(tmp_path / "no-advisory.md"))
    monkeypatch.setattr(dream.httpx, "post", lambda url, **kw: _Resp())
    return tmp_path / "dreams" / "records", memory


def _put(folder: Path, name: str, entry: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.json"
    p.write_text(json.dumps(entry), encoding="utf-8")
    return p


def _direct(dream_id: str, ts: str, summary: str = "the brief", session: str = "dream-2026-11-17") -> dict:
    """What write_dream wrote before this change (no schema_version)."""
    return {"id": dream_id, "session_id": session, "agent_id": "dreamer", "summary": summary,
            "key_facts": [f"Dream covering {ts}"], "timestamp": ts, "provider": "anthropic"}


def _save(memory_id: str, ts: str, summary: str = "the brief", session: str = "dream-2026-11-17",
          key_facts=None) -> dict:
    """What the server's /writeback stores (schema_version 3)."""
    return {"id": memory_id, "session_id": session, "agent_id": "dreamer", "summary": summary,
            "key_facts": key_facts if key_facts is not None else [], "timestamp": ts,
            "created_at": 1.0, "source": "agent", "category": "session_log", "schema_version": 3}


# ── write_dream ──

def test_write_dream_keeps_its_record_outside_the_memory_folder(dirs):
    records, memory = dirs
    dream_id = dream.write_dream("brief body", [{"agent_id": "cc"}],
                                 datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert (records / f"{dream_id}.json").is_file()
    assert not memory.exists() or not list(memory.glob("*.json"))


def test_a_walker_of_the_dreamer_memory_folder_finds_no_direct_copy(dirs):
    records, memory = dirs
    memory.mkdir(parents=True)
    dream.write_dream("brief body", [{"agent_id": "cc"}], datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert list(iter_memory_entries(memory)) == []


def test_the_window_moves_from_the_new_record(dirs):
    records, memory = dirs
    assert dream.get_last_dream_time() is None
    dream.write_dream("brief body", [{"agent_id": "cc"}], datetime(2026, 11, 17, tzinfo=timezone.utc))
    last = dream.get_last_dream_time()
    assert last is not None and (datetime.now(timezone.utc) - last).total_seconds() < 60


# ── get_last_dream_time reads both places ──

def test_last_dream_time_is_the_newest_across_records_and_memory_folder(dirs):
    records, memory = dirs
    _put(memory, "a" * 16, _direct("a" * 16, "2026-11-15T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "2026-11-16T10:15:00+00:00"))
    _put(records, "b" * 16, _direct("b" * 16, "2026-11-14T10:15:00+00:00"))
    assert dream.get_last_dream_time() == datetime(2026, 11, 16, 10, 15, tzinfo=timezone.utc)
    _put(records, "c" * 16, _direct("c" * 16, "2026-11-17T10:15:00+00:00"))
    assert dream.get_last_dream_time() == datetime(2026, 11, 17, 10, 15, tzinfo=timezone.utc)


def test_last_dream_time_with_neither_folder_is_none(dirs):
    assert dream.get_last_dream_time() is None


# ── finding the old twins ──

def test_a_direct_copy_with_an_indexed_twin_is_found_and_the_save_is_not(dirs):
    _, memory = dirs
    copy = _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "2026-11-17T10:15:01+00:00"))
    copies, notes = dream.find_direct_copies()
    assert copies == [copy] and notes == []


def test_a_twin_by_key_facts_counts_when_the_summary_differs(dirs):
    _, memory = dirs
    d = _direct("a" * 16, "2026-11-17T10:15:00+00:00", summary="brief as written")
    copy = _put(memory, "a" * 16, d)
    _put(memory, "srv1", _save("srv1", "x", summary="brief as stored", key_facts=d["key_facts"]))
    assert dream.find_direct_copies()[0] == [copy]


def test_a_direct_copy_without_a_twin_stays_and_is_reported(dirs):
    _, memory = dirs
    _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "x", summary="another dream", session="dream-2026-11-16"))
    copies, notes = dream.find_direct_copies()
    assert copies == [] and len(notes) == 1 and "no indexed twin" in notes[0]


def test_a_same_text_save_from_another_session_is_not_a_twin(dirs):
    _, memory = dirs
    _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "x", session="dream-2026-11-16"))
    assert dream.find_direct_copies()[0] == []


def test_records_that_are_not_dreamer_copies_are_left_alone(dirs):
    _, memory = dirs
    other = _direct("a" * 16, "t")
    other["agent_id"] = "cc"
    _put(memory, "a" * 16, other)                                  # wrong agent
    _put(memory, "not-its-id", _direct("b" * 16, "t"))             # name != id
    (memory / "broken.json").write_text("{", encoding="utf-8")     # unreadable
    _put(memory, "srv1", _save("srv1", "t"))
    copies, notes = dream.find_direct_copies()
    assert copies == [] and len(notes) == 3


# ── the one-time move ──

def test_dry_run_moves_nothing(dirs, capsys):
    records, memory = dirs
    copy = _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "t"))
    assert dream.move_direct_copies(apply=False) == 0
    assert copy.exists() and not records.exists()
    assert "would move " + copy.name in capsys.readouterr().out


def test_apply_moves_the_copy_and_the_window_holds(dirs):
    records, memory = dirs
    copy = _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    save = _put(memory, "srv1", _save("srv1", "2026-11-17T10:14:00+00:00"))
    before = dream.get_last_dream_time()
    assert dream.move_direct_copies(apply=True) == 0
    assert not copy.exists() and (records / copy.name).is_file() and save.exists()
    assert dream.get_last_dream_time() == before
    assert [m[0] for m in iter_memory_entries(memory)] == ["srv1"]


def test_apply_never_overwrites_a_record(dirs, capsys):
    records, memory = dirs
    copy = _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "t"))
    _put(records, "a" * 16, {"keep": "me"})
    assert dream.move_direct_copies(apply=True) == 1
    assert copy.exists()
    assert json.loads((records / copy.name).read_text()) == {"keep": "me"}
    assert "SKIP" in capsys.readouterr().out


def test_apply_without_move_flag_is_refused(monkeypatch):
    monkeypatch.setattr(dream.sys, "argv", ["mnemo-dream.py", "--apply"])
    with pytest.raises(SystemExit) as e:
        dream.main()
    assert e.value.code == 2
