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


@pytest.fixture(autouse=True)
def _never_the_real_folders(monkeypatch, tmp_path):
    """Every test, even one that calls main(), sees only tmp folders: a fail-before
    run on code without a guard must not reach the real dreamer folder (it did
    once: 103 real files moved, restored from a snapshot)."""
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path / "dreams")
    monkeypatch.setattr(dream, "DREAMER_MEMORY_DIR", tmp_path / "agents" / "dreamer" / "memory")
    monkeypatch.setattr(dream, "AGENTS_ROOT", tmp_path / "agents")

    def _no_network(url, **kw):
        raise AssertionError(f"test reached the network: {url}")
    monkeypatch.setattr(dream.httpx, "post", _no_network)   # dirs / live install their own


def _must_not_run(monkeypatch):
    """For tests of a refused command line: reaching any action is a failure."""
    def boom(*a, **kw):
        raise AssertionError("a refused command line reached an action")
    monkeypatch.setattr(dream, "move_direct_copies", boom)
    monkeypatch.setattr(dream, "run_with_one_retry", boom)


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
    assert "would demote + move " + copy.name in capsys.readouterr().out


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
    _must_not_run(monkeypatch)
    monkeypatch.setattr(dream.sys, "argv", ["mnemo-dream.py", "--apply"])
    with pytest.raises(SystemExit) as e:
        dream.main()
    assert e.value.code == 2


# ── 13c: a copy already in the vec index leaves it before it moves ──
# A backfill or reindex over the dreamer folder (before this version) indexed
# some copies. Moving such a file would leave its vector behind: recall cannot
# read the missing file to drop it, and no walker ever visits the row again.

from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agentb.config import (  # noqa: E402
    AgentBConfig, AgentConfig, CacheConfig, ClassificationConfig,
    DEFAULT_PERSONAS, ProviderConfig, ResilientProviderConfig, ServerConfig,
)
from agentb.vec import EMBED_DIM  # noqa: E402

_VEC = [0.0] * EMBED_DIM
_VEC[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class _Embed:
    active_label = "fake/embed"
    status = _STATUS

    @property
    def primary(self):          # /vec/backfill embeds with the primary directly
        return self

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(_VEC)

    async def health_check(self):
        return True


class _Reason:
    active_label = "fake/reason"
    status = _STATUS

    async def generate(self, *a, **kw):
        return "x"

    async def health_check(self):
        return True


@pytest.fixture
def live(monkeypatch, tmp_path):
    """A real server with a dreamer tenant; the dreamer's httpx.post goes to it.
    Yields (client, memory_dir, records_dir, copy_path, vec_store)."""
    data = tmp_path / "agents" / "dreamer"
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        cache=CacheConfig(), classification=ClassificationConfig(enabled=False),
        server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path / "default"), personas=dict(DEFAULT_PERSONAS),
        agents={"dreamer": AgentConfig(data_dir=str(data))},
    )
    with patch("agentb.server.create_resilient_embedding", return_value=_Embed()), \
         patch("agentb.server.create_resilient_reasoning", return_value=_Reason()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as client:
            memory = data / "memory"
            monkeypatch.setattr(dream, "DREAM_DIR", tmp_path / "dreams")
            monkeypatch.setattr(dream, "DREAMER_MEMORY_DIR", memory)
            monkeypatch.setenv("MNEMO_URL", "http://mnemo.test")
            monkeypatch.setattr(dream.httpx, "post", lambda url, timeout=None, **kw:
                                client.post(url.replace("http://mnemo.test", ""), **kw))
            r = client.post("/writeback", json={"session_id": "dream-2026-11-17", "agent_id": "dreamer",
                                                "summary": "the brief", "batch": True})
            assert r.status_code == 200, r.text
            copy = _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
            vec = client.app.state.tenants.get("dreamer")["vec"]
            yield client, memory, tmp_path / "dreams" / "records", copy, vec


def _index_the_copy(client, vec, copy):
    """What an earlier backfill over the dreamer folder did."""
    r = client.post("/vec/backfill", params={"agent_id": "dreamer"})
    assert r.status_code == 200, r.text
    assert vec.has(copy.stem)


def test_apply_takes_an_indexed_copy_out_of_the_index_before_moving_it(live, capsys):
    client, memory, records, copy, vec = live
    _index_the_copy(client, vec, copy)
    assert dream.move_direct_copies(apply=True) == 0
    assert not copy.exists() and (records / copy.name).is_file()
    assert not vec.has(copy.stem)
    assert "moved 1, taken out of the index 1, left 0" in capsys.readouterr().out


def test_dry_run_counts_indexed_copies_and_changes_nothing(live, capsys):
    client, memory, records, copy, vec = live
    _index_the_copy(client, vec, copy)
    assert dream.move_direct_copies(apply=False) == 0
    out = capsys.readouterr().out
    assert "of which in the vec index: 1" in out and "(in the index)" in out
    assert copy.exists() and vec.has(copy.stem)
    assert "superseded_by" not in json.loads(copy.read_text())


def test_an_unindexed_copy_is_demoted_and_moved_too(live, capsys):
    client, memory, records, copy, vec = live
    assert dream.move_direct_copies(apply=True) == 0
    assert (records / copy.name).is_file()
    assert json.loads((records / copy.name).read_text())["superseded_by"].startswith("demoted:dreamer:")
    assert "moved 1, taken out of the index 0, left 0" in capsys.readouterr().out


def test_a_rerun_moves_a_copy_an_earlier_run_demoted_but_did_not_move(live):
    client, memory, records, copy, vec = live
    _index_the_copy(client, vec, copy)
    r = client.post("/memories/demote", json={"memory_id": copy.stem, "agent_id": "dreamer",
                                              "reason": "first run", "by": "dreamer"})
    assert r.json()["demoted"] is True and not vec.has(copy.stem)
    assert dream.move_direct_copies(apply=True) == 0
    assert (records / copy.name).is_file()


def test_a_copy_still_in_the_index_after_demote_stays(live, capsys):
    client, memory, records, copy, vec = live
    _index_the_copy(client, vec, copy)
    rec = json.loads(copy.read_text())
    rec["superseded_by"] = "demoted:someone:1"      # demote then answers 'already superseded'
    copy.write_text(json.dumps(rec))
    assert dream.move_direct_copies(apply=True) == 1
    assert copy.exists() and vec.has(copy.stem)
    out = capsys.readouterr().out
    assert "still in the index after demote" in out and "left 1" in out


def test_a_failed_demote_leaves_the_copy(dirs, monkeypatch, capsys):
    records, memory = dirs
    copy = _put(memory, "a" * 16, _direct("a" * 16, "2026-11-17T10:15:00+00:00"))
    _put(memory, "srv1", _save("srv1", "t"))

    class _Forbidden:
        status_code = 403
        text = "Token is scoped to agent 'cc'"
    monkeypatch.setattr(dream.httpx, "post", lambda url, **kw: _Forbidden())
    assert dream.move_direct_copies(apply=True) == 1
    assert copy.exists() and not (records / copy.name).exists()
    assert "LEFT" in capsys.readouterr().out


def test_server_unreachable_moves_nothing(dirs, monkeypatch, capsys):
    records, memory = dirs
    copies = [_put(memory, c * 16, _direct(c * 16, "2026-11-17T10:15:00+00:00")) for c in "ab"]
    _put(memory, "srv1", _save("srv1", "t"))

    def _down(url, **kw):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(dream.httpx, "post", _down)
    assert dream.move_direct_copies(apply=True) == 1
    assert all(c.exists() for c in copies)
    out = capsys.readouterr().out
    assert "server unreachable" in out and "moved 0, taken out of the index 0, left 2" in out


def test_dry_run_with_apply_is_refused(monkeypatch):
    _must_not_run(monkeypatch)
    monkeypatch.setattr(dream.sys, "argv",
                        ["mnemo-dream.py", "--move-direct-copies", "--dry-run", "--apply"])
    with pytest.raises(SystemExit) as e:
        dream.main()
    assert e.value.code == 2


# ── 13b: a failed /writeback is retried on the next run ──
# Before: a dream whose /writeback failed was never searchable, and the only
# alarm was a log line. Now the record is marked and the next run re-POSTs it.

class _Down:
    status_code = 503
    text = ""


def _wb_down(url, **kw):
    return _Down()


def _record(records: Path) -> dict:
    (f,) = records.glob("*.json")
    return json.loads(f.read_text(encoding="utf-8"))


def test_a_failed_writeback_marks_the_record(dirs, monkeypatch):
    records, _ = dirs
    monkeypatch.setattr(dream.httpx, "post", _wb_down)
    dream.write_dream("brief body", [{"agent_id": "cc"}], datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert _record(records)["writeback"] == "failed"


def test_a_failed_retry_keeps_the_mark_and_a_clean_record_is_not_sent(dirs, monkeypatch):
    records, _ = dirs
    dream.write_dream("an earlier brief", [{"agent_id": "cc"}], datetime(2026, 11, 16, tzinfo=timezone.utc))
    (clean,) = records.glob("*.json")  # a complete record whose save landed: no mark
    monkeypatch.setattr(dream.httpx, "post", _wb_down)
    dream.write_dream("brief body", [{"agent_id": "cc"}], datetime(2026, 11, 17, tzinfo=timezone.utc))
    sent = []
    monkeypatch.setattr(dream.httpx, "post", lambda url, **kw: sent.append(kw["json"]) or _Down())
    dream.retry_failed_writebacks()
    assert [p["summary"] for p in sent] == ["brief body"]
    marked = [json.loads(f.read_text())["id"] for f in records.glob("*.json")
              if json.loads(f.read_text()).get("writeback") == "failed"]
    assert len(marked) == 1 and marked[0] != clean.stem


def test_the_next_run_saves_a_marked_dream_and_the_window_holds(live, monkeypatch):
    client, memory, records, _, vec = live
    monkeypatch.setenv("MNEMO_RULEKEEPER_ADVISORY", str(records.parent / "no-advisory.md"))
    to_server = dream.httpx.post
    monkeypatch.setattr(dream.httpx, "post", _wb_down)
    dream.write_dream("tonight's brief", [{"agent_id": "cc"}], datetime(2026, 11, 17, tzinfo=timezone.utc))
    window = dream.get_last_dream_time()
    before = set(memory.glob("*.json"))
    monkeypatch.setattr(dream.httpx, "post", to_server)
    dream.retry_failed_writebacks()
    assert "writeback" not in _record(records)
    (saved,) = set(memory.glob("*.json")) - before
    save = json.loads(saved.read_text(encoding="utf-8"))
    assert save["summary"] == "tonight's brief" and vec.has(saved.stem)
    assert save["timestamp"] == _record(records)["timestamp"]
    assert dream.get_last_dream_time() == window


def test_a_resend_of_a_save_that_landed_makes_no_second_copy(live, monkeypatch):
    """A /writeback that landed but timed out on the way back is marked failed;
    its re-send maps to the same memory id on the server."""
    client, memory, records, _, vec = live
    monkeypatch.setenv("MNEMO_RULEKEEPER_ADVISORY", str(records.parent / "no-advisory.md"))
    dream.write_dream("tonight's brief", [{"agent_id": "cc"}], datetime(2026, 11, 17, tzinfo=timezone.utc))
    after_first = sorted(memory.glob("*.json"))
    (f,) = records.glob("*.json")
    f.write_text(json.dumps({**_record(records), "writeback": "failed"}), encoding="utf-8")
    dream.retry_failed_writebacks()
    assert sorted(memory.glob("*.json")) == after_first
    assert "writeback" not in _record(records)


def test_main_retries_before_the_run_and_never_on_a_dry_run(monkeypatch):
    for argv, expected in ((["mnemo-dream.py"], ["retry", "run"]),
                           (["mnemo-dream.py", "--dry-run"], ["run"])):
        events = []
        monkeypatch.setattr(dream.sys, "argv", argv)
        monkeypatch.setattr(dream, "_require_claude", lambda: None)
        monkeypatch.setattr(dream, "retry_failed_writebacks", lambda: events.append("retry"))
        monkeypatch.setattr(dream, "run_with_one_retry", lambda run, **kw: events.append("run"))
        dream.main()
        assert events == expected, argv
