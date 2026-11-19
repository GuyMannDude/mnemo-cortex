"""A vec row whose record is gone is not served (item 14 A, design #6858,
ruling #6864).

The record file of a memory can vanish while its vector stays: a hand
delete, a stick sync that removed it on this host, a memory folder that is
not there right now. /context (VEC, LEX and the recent lens), /preflight and
the write-time dedup gate served or counted such a row from the index text
alone. Now each drops it; /context and /preflight warn per id for the first
few, then one summary line per request.

source_file is stored ABSOLUTE, so the NEGATIVE half matters as much: a data
dir that moved still serves every row (found at <memory_dir>/<basename>),
and its superseded_by marks still hold.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from unittest.mock import patch

from tests.test_recall_drops_superseded import (
    VEC, NearestIsDeadEmbedding, RecordingReasoning, _client, _context, _damage, _save,
    _warnings,
)

from agentb.vec import is_superseded, record_file


def _record(tmp_path: Path, memory_id: str, tenant: str = "cody") -> Path:
    return tmp_path / tenant / "memory" / f"{memory_id}.json"


def _orphan(tmp_path: Path, client, memory_id: str) -> None:
    """Delete the record and leave its vector: what a hand delete or a stick
    sync on this host leaves behind."""
    _record(tmp_path, memory_id).unlink()
    assert client.app.state.tenants.get("cody")["vec"].has(memory_id)


def _orphan_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "no record on disk" in r.getMessage()]


# ── helpers ────────────────────────────────────────────────────────────────

def test_record_file_finds_the_stored_path_then_the_moved_one(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "a.json").write_text("{}")
    stored = (tmp_path / "old-home" / "memory" / "a.json").as_posix()
    assert record_file((mem / "a.json").as_posix(), mem) == mem / "a.json"
    assert record_file(stored, mem) == mem / "a.json"          # moved data dir
    assert record_file(stored) is None                         # no memory_dir: no rebase
    assert record_file((tmp_path / "gone.json").as_posix(), mem) is None
    assert record_file(None, mem) is None
    assert record_file("", mem) is None


def test_is_superseded_reads_the_mark_and_fails_open(tmp_path):
    dead, live, junk = (tmp_path / n for n in ("dead.json", "live.json", "junk.json"))
    dead.write_text(json.dumps({"superseded_by": "demoted:cc:1"}))
    live.write_text(json.dumps({"summary": "x"}))
    junk.write_text("{not json")
    assert is_superseded(dead) and is_superseded(dead.as_posix())
    assert not is_superseded(live)
    assert not is_superseded(junk)
    assert not is_superseded(None)
    assert not is_superseded(tmp_path / "missing.json")


# ── /context ───────────────────────────────────────────────────────────────

def test_context_vec_lane_drops_an_orphan_and_warns_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agentb")
    with _client(tmp_path) as client:
        live = _save(client, "the vault lives on the-north-box")
        gone = _save(client, "the vault lives on the-old-box")
        _orphan(tmp_path, client, gone)
        for _ in range(2):
            served = _context(client, "where does the vault live")
            assert live in served       # presence: the lane did serve
            assert gone not in served
    warnings = _orphan_warnings(caplog)
    assert len(warnings) == 1 and gone in warnings[0]


def test_context_lex_lane_drops_an_orphan(tmp_path):
    with _client(tmp_path) as client:
        live = _save(client, "the hinge part is zq7731-live in the shop list")
        gone = _save(client, "the hinge part is zq7731-gone in the shop list")
        _orphan(tmp_path, client, gone)
        store = client.app.state.tenants.get("cody")["vec"]
        with patch.object(store, "search", return_value=[]), \
             patch.object(store, "newest", return_value=[]):
            served = _context(client, "which part is zq7731-live or zq7731-gone")
        assert live in served
        assert gone not in served


def test_context_recent_lens_drops_an_orphan(tmp_path):
    with _client(tmp_path) as client:
        live = _save(client, "the vault lives on the-north-box")
        gone = _save(client, "the vault lives on the-old-box")
        _orphan(tmp_path, client, gone)
        store = client.app.state.tenants.get("cody")["vec"]
        # Blind the kNN so only the recency pool can bring these in.
        with patch.object(store, "search", return_value=[]):
            r = client.post("/context", json={"prompt": "where does the vault live",
                                              "agent_id": "cody", "max_results": 10,
                                              "mode": "recent"})
        assert r.status_code == 200, r.text
        served = [c["memory_id"] for c in r.json()["chunks"]]
        assert live in served
        assert gone not in served


def test_many_orphans_warn_per_id_for_five_then_one_summary_line(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agentb")
    with _client(tmp_path) as client:
        live = _save(client, "the vault lives on the-north-box")
        gone = [_save(client, f"the vault lives on the-old-box-{i}") for i in range(8)]
        for memory_id in gone:
            _orphan(tmp_path, client, memory_id)
        first = _context(client, "where does the vault live")
        after_first = list(_orphan_warnings(caplog))
        second = _context(client, "where does the vault live")
        assert live in first and live in second
        assert not set(gone) & (set(first) | set(second))
    per_id = [w for w in after_first if w.startswith("Vec row ")]
    summary = [w for w in after_first if "more vec rows" in w]
    assert len(per_id) == 5
    assert len(summary) == 1 and summary[0].startswith("3 more vec rows")
    assert _orphan_warnings(caplog) == after_first      # second request: silent


def test_a_row_with_no_source_file_is_served_as_before(tmp_path):
    with _client(tmp_path) as client:
        _save(client, "the vault lives on the-north-box")
        store = client.app.state.tenants.get("cody")["vec"]
        store.upsert("nosrc0000000001", "the vault lives on the-no-source-box", list(VEC))
        assert "nosrc0000000001" in _context(client, "where does the vault live")


def test_an_unparseable_record_stays_live(tmp_path):
    with _client(tmp_path) as client:
        junk = _save(client, "the vault lives on the-junk-box")
        _record(tmp_path, junk).write_text("{not json")
        assert junk in _context(client, "where does the vault live")


# ── /preflight ─────────────────────────────────────────────────────────────

def test_preflight_never_shows_an_orphan_to_the_reasoner(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agentb")
    reasoner = RecordingReasoning()
    with _client(tmp_path, reasoner, NearestIsDeadEmbedding()) as client:
        _save(client, "the vault lives on the-north-box")
        _save(client, "the vault key hangs by the-north-door")
        _save(client, "the vault alarm code is on the-north-card")
        gone = _save(client, "the vault lives on the-old-box")     # the nearest hit
        _orphan(tmp_path, client, gone)
        r = client.post("/preflight", json={"prompt": "where is the vault",
                                            "draft_response": "the-north-box",
                                            "agent_id": "cody"})
        assert r.status_code == 200, r.text
    memory_block = reasoner.prompts[0].split("USER'S PROMPT:")[0]
    assert "the-old-box" not in memory_block
    assert memory_block.count("[VEC] ") == 2              # still the two nearest live
    assert any(gone in w for w in _orphan_warnings(caplog))


# ── write-time dedup gate ──────────────────────────────────────────────────

def _plain_save(client, session_id: str, text: str):
    return client.post("/writeback", json={
        "session_id": session_id, "summary": text, "agent_id": "cody",
        "category": "doctrine", "source": "inferred"})


def test_an_orphan_does_not_hold_a_new_save(tmp_path):
    text = "the vault master key hangs behind the north workshop door"
    with _client(tmp_path) as client:
        first = _plain_save(client, "s1", text)
        assert first.json()["status"] != "held", first.text
        # Control: while the record is there, the same text is held.
        held = _plain_save(client, "s2", text)
        assert held.json()["status"] == "held", held.text
        _orphan(tmp_path, client, first.json()["memory_id"])
        again = _plain_save(client, "s3", text)
        assert again.json()["status"] != "held", again.text
        assert again.json()["memory_id"]


# ── NEGATIVE: a data dir that moved is still served ────────────────────────

def test_a_moved_data_dir_is_still_served_and_its_demotions_hold(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agentb")
    old = tmp_path / "old"
    text = "the vault master key hangs behind the north workshop door"
    reasoner = RecordingReasoning()
    with _client(old, reasoner) as client:
        live = _save(client, "the vault lives on the-north-box")
        dead = _save(client, "the vault lives on the-old-box")
        _damage(old, client, dead)
        assert _plain_save(client, "s1", text).json()["status"] != "held"
        stored = client.app.state.tenants.get("cody")["vec"].search(list(VEC), top_k=5)
    # The rows point into the OLD dir; move the whole data dir away.
    assert stored and all(h.source_file.startswith(old.as_posix()) for h in stored)
    new = tmp_path / "new"
    shutil.move(str(old), str(new))
    assert not old.exists()
    with _client(new, reasoner) as client:
        r = client.post("/context", json={"prompt": "where does the vault live",
                                          "agent_id": "cody", "max_results": 10})
        assert r.status_code == 200, r.text
        chunks = {c["memory_id"]: c for c in r.json()["chunks"]}
        r = client.post("/preflight", json={"prompt": "where is the vault",
                                            "draft_response": "the-north-box",
                                            "agent_id": "cody"})
        assert r.status_code == 200, r.text
        held = _plain_save(client, "s2", text)
    # Served from the INDEX with its record read (not found by the disk walk,
    # which also finds it when every VEC row is dropped).
    assert chunks[live]["cache_tier"] == "VEC"
    assert chunks[live]["category"] == "doctrine"
    # Dropped as DEMOTED (its superseded_by read through the rebase), not as an orphan.
    assert dead not in chunks
    assert _warnings(caplog, dead) >= 1
    assert _orphan_warnings(caplog) == []
    # /preflight and the dedup gate find the moved records too.
    assert "the vault lives on the-north-box" in reasoner.prompts[-1].split("USER'S PROMPT:")[0]
    assert held.json()["status"] == "held", held.text
