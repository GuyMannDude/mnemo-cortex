"""Tests for Trajectory Learning Phase 1 (Mnemo v4.5.0)."""
from __future__ import annotations

import json
from pathlib import Path

from agentb.vec import EMBED_DIM
from agentb.trajectory import (
    TrajectoryStore,
    sanitize_task_type,
    embedding_text,
)


def _vec_along(axis: int, magnitude: float = 1.0) -> list[float]:
    """A unit-ish vector pointing along one axis — distinct axes are far apart,
    so 'query along axis N' reliably retrieves the trajectory saved at axis N."""
    v = [0.0] * EMBED_DIM
    v[axis] = magnitude
    return v


def _save(store: TrajectoryStore, axis: int, *, task_type="bus_debug", rating=5, desc="fix the bus path"):
    return store.save(
        agent_id="cc",
        task_type=task_type,
        task_description=desc,
        steps=[{"action": "grep config", "tool_used": "bash", "result_summary": "found stale path"}],
        outcome="bus reads correct db",
        rating=rating,
        embedding=_vec_along(axis),
    )


# ── sanitize_task_type ──

def test_sanitize_blocks_path_traversal():
    assert sanitize_task_type("../../etc/passwd") == "etc_passwd"
    assert sanitize_task_type("bus/debug") == "bus_debug"
    assert sanitize_task_type("Shopify Fix!") == "shopify_fix"
    assert sanitize_task_type("") == "untagged"
    assert sanitize_task_type("   ") == "untagged"
    assert sanitize_task_type("___") == "untagged"


def test_sanitize_keeps_safe_chars():
    assert sanitize_task_type("bus_debug") == "bus_debug"
    assert sanitize_task_type("security-triage") == "security-triage"


# ── embedding_text ──

def test_embedding_text_combines_signal():
    text = embedding_text("the goal", "the outcome", [{"action": "step one"}, {"action": "step two"}])
    assert "the goal" in text
    assert "the outcome" in text
    assert "step one" in text and "step two" in text


def test_embedding_text_truncates_oversize():
    from agentb.trajectory import MAX_EMBED_INPUT_CHARS
    text = embedding_text("x" * 99999, "", [])
    assert len(text) <= MAX_EMBED_INPUT_CHARS


# ── save: JSONL is disk truth ──

def test_save_appends_jsonl(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    rec = _save(store, 0)
    jsonl = tmp_path / "traj" / "bus_debug.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["id"] == rec["id"]
    assert on_disk["rating"] == 5
    assert on_disk["task_type"] == "bus_debug"
    assert on_disk["created_at"] > 0


def test_save_is_append_only(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    _save(store, 0)
    _save(store, 1)
    lines = (tmp_path / "traj" / "bus_debug.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert store.count() == 2


def test_save_sanitized_filename_keeps_original_category(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    store.save(
        agent_id="cc", task_type="Shopify Fix", task_description="d",
        steps=[{"action": "a"}], outcome="o", rating=5, embedding=_vec_along(0),
    )
    # filename sanitized…
    assert (tmp_path / "traj" / "shopify_fix.jsonl").exists()
    # …but the record + vec category keep the original task_type
    rec = json.loads((tmp_path / "traj" / "shopify_fix.jsonl").read_text().strip())
    assert rec["task_type"] == "Shopify Fix"


# ── recall: similarity ──

def test_recall_returns_nearest(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    r0 = _save(store, 0, desc="trajectory zero")
    _save(store, 5, desc="trajectory five")
    hits = store.recall(_vec_along(0), max_results=1)
    assert len(hits) == 1
    assert hits[0]["id"] == r0["id"]
    assert "_score" in hits[0] and "similarity" in hits[0]["_score"]


def test_recall_empty_store(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    assert store.recall(_vec_along(0)) == []


# ── recall: min_rating filter ──

def test_recall_min_rating_excludes_low(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    _save(store, 0, rating=2, desc="low quality at axis 0")
    good = _save(store, 1, rating=5, desc="high quality at axis 1")
    # Query nearest the LOW one, but min_rating must exclude it and fall through
    # to the acceptable one.
    hits = store.recall(_vec_along(0), min_rating=4, max_results=5)
    ids = [h["id"] for h in hits]
    assert good["id"] in ids
    assert all(h["rating"] >= 4 for h in hits)


def test_recall_min_rating_default_is_three(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    _save(store, 0, rating=2)
    hits = store.recall(_vec_along(0))  # default min_rating=3
    assert hits == []


# ── recall: task_type filter ──

def test_recall_task_type_filter(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    _save(store, 0, task_type="bus_debug", desc="bus one")
    other = _save(store, 1, task_type="shopify_fix", desc="shopify one")
    hits = store.recall(_vec_along(1), task_type="shopify_fix", max_results=5)
    assert len(hits) == 1
    assert hits[0]["id"] == other["id"]
    assert hits[0]["task_type"] == "shopify_fix"


# ── recall: ranking favors rating/recency on near-ties ──

def test_recall_rating_breaks_near_tie(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    # Both at the SAME axis (identical similarity); higher rating must rank first.
    low = _save(store, 3, rating=3, desc="same spot low")
    high = _save(store, 3, rating=5, desc="same spot high")
    hits = store.recall(_vec_along(3), min_rating=1, max_results=2)
    assert hits[0]["id"] == high["id"]
    assert hits[1]["id"] == low["id"]


# ── crash safety: a torn final line never hides earlier entries ──

def test_recall_survives_malformed_line(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    good = _save(store, 0, desc="good record")
    # Simulate a crash mid-append: a partial JSON line at the end of the file.
    jsonl = tmp_path / "traj" / "bus_debug.jsonl"
    with jsonl.open("a", encoding="utf-8") as f:
        f.write('{"id": "torn", "rating": 5, "task_typ')  # no newline, truncated
    # Fresh store (cold cache) must still parse the good record and skip the torn one.
    store2 = TrajectoryStore(tmp_path / "traj")
    loaded = store2._load("bus_debug")
    assert good["id"] in loaded
    assert "torn" not in loaded


def test_recall_sees_save_within_same_mtime_tick(tmp_path: Path):
    """Regression (code review #1): a second save landing in the same coarse
    mtime tick must not be hidden by the parsed-record cache. Before the
    cache-invalidation fix, recall returned the stale cached set and silently
    dropped the new trajectory even though the vec index returned its id."""
    import os
    store = TrajectoryStore(tmp_path / "traj")
    _save(store, 0, desc="first")
    store.recall(_vec_along(0), max_results=5)  # warm the cache
    path = tmp_path / "traj" / "bus_debug.jsonl"
    mtime = path.stat().st_mtime
    second = _save(store, 1, desc="second")
    os.utime(path, (mtime, mtime))  # force mtime back: simulate same-tick save
    hits = store.recall(_vec_along(1), min_rating=1, max_results=5)
    assert second["id"] in [h["id"] for h in hits]


def test_task_types_lists_files(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    _save(store, 0, task_type="bus_debug")
    _save(store, 1, task_type="shopify_fix")
    assert store.task_types() == ["bus_debug", "shopify_fix"]


# ── v4.7: provenance fields ──

def test_save_defaults_are_hand_saved_provenance(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    rec = _save(store, 0)
    assert rec["source"] == "agent"
    assert rec["derived_from"] is None
    assert rec["evidence_source"] is None


def test_save_dreamer_provenance_lands_on_disk(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    store.save(
        agent_id="cc", task_type="live-store-migration", task_description="d",
        steps=[{"action": "a"}], outcome="o", rating=4, embedding=_vec_along(0),
        derived_from="failure", source="dreamer",
        evidence_source="dream:2026-07-02 cc-jsonl-8086924f",
    )
    on_disk = json.loads(
        (tmp_path / "traj" / "live-store-migration.jsonl").read_text().strip()
    )
    assert on_disk["derived_from"] == "failure"
    assert on_disk["source"] == "dreamer"
    assert on_disk["evidence_source"] == "dream:2026-07-02 cc-jsonl-8086924f"


# ── v4.7: recall reinforcement stats ──

def test_recall_bumps_stats_sidecar(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    rec = _save(store, 0)
    _save(store, 5)  # far axis — not returned by the axis-0 query
    store.recall(_vec_along(0), max_results=1)
    stats = json.loads((tmp_path / "traj" / "recall_stats.json").read_text())
    assert stats[rec["id"]]["recall_count"] == 1
    assert stats[rec["id"]]["last_recalled"] > 0
    assert len(stats) == 1  # the un-returned trajectory got no bump
    store.recall(_vec_along(0), max_results=1)
    stats = json.loads((tmp_path / "traj" / "recall_stats.json").read_text())
    assert stats[rec["id"]]["recall_count"] == 2


def test_recall_empty_result_writes_no_stats(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    assert store.recall(_vec_along(0)) == []
    assert not (tmp_path / "traj" / "recall_stats.json").exists()


def test_corrupt_stats_sidecar_never_breaks_recall(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "traj")
    rec = _save(store, 0)
    (tmp_path / "traj" / "recall_stats.json").write_text("{not json")
    hits = store.recall(_vec_along(0), max_results=1)
    assert hits and hits[0]["id"] == rec["id"]
    # stats were reset, then bumped for this recall
    stats = json.loads((tmp_path / "traj" / "recall_stats.json").read_text())
    assert stats[rec["id"]]["recall_count"] == 1


def test_wrong_shape_stats_sidecar_never_breaks_recall(tmp_path: Path):
    """Valid JSON, wrong shape — scalar entries and junk counts must be
    coerced, never raised (a raise here would discard built recall results)."""
    store = TrajectoryStore(tmp_path / "traj")
    rec = _save(store, 0)
    (tmp_path / "traj" / "recall_stats.json").write_text(
        json.dumps({rec["id"]: 5, "other": {"recall_count": "x"}}))
    hits = store.recall(_vec_along(0), max_results=1)
    assert hits and hits[0]["id"] == rec["id"]
    stats = json.loads((tmp_path / "traj" / "recall_stats.json").read_text())
    assert stats[rec["id"]]["recall_count"] == 1  # scalar entry coerced, then bumped
