"""Stage 0.7 — trajectory strategy distillation (v4.7, Trajectory Phase 2).

Covers the deterministic seams around the LLM call: session-stream grouping,
section building (recency-first cap), judge-output validation, and the
stale-trajectory curation flag pass. The distill call itself is exercised by
the nightly --dry-run, not unit tests.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

# The dreamer is a top-level script with a hyphen in its name — load it by path.
_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream_strategies", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)


def _batch(agent: str, sid: str, i: int, summary: str = "activity") -> dict:
    return {
        "agent_id": agent,
        "session_id": sid,
        "timestamp": f"2026-07-01T{i:02d}:00:00+00:00",
        "summary": f"{agent.upper()} session activity (auto-sync from JSONL). {summary}",
        "key_facts": [f"{agent.upper()} invoked tool: Bash"],
    }


# ── _session_streams ──

def test_session_streams_groups_by_agent_and_session():
    mems = (
        [_batch("cc", "cc-jsonl-aaa", i) for i in range(3)]
        + [_batch("rocky", "rocky-jsonl-bbb", i) for i in range(4)]
    )
    streams = dream._session_streams(mems)
    assert set(streams) == {"cc", "rocky"}
    assert len(streams["cc"]["cc-jsonl-aaa"]) == 3
    assert len(streams["rocky"]["rocky-jsonl-bbb"]) == 4


def test_session_streams_drops_slivers_and_non_jsonl():
    mems = [_batch("cc", "cc-jsonl-aaa", i) for i in range(3)]
    mems += [_batch("cc", "cc-jsonl-tiny", 1)]  # 1 batch < STRATEGY_MIN_BATCHES
    mems += [{  # intentional save — not a stream
        "agent_id": "cc", "session_id": "cc-2026-07-01-10-00-00",
        "timestamp": "2026-07-01T10:00:00+00:00", "summary": "shipped v4.6.0",
        "key_facts": [],
    }]
    streams = dream._session_streams(mems)
    assert set(streams["cc"]) == {"cc-jsonl-aaa"}


def test_session_streams_orders_batches_chronologically():
    mems = [_batch("cc", "cc-jsonl-aaa", i) for i in (7, 3, 5)]
    streams = dream._session_streams(mems)
    stamps = [m["timestamp"] for m in streams["cc"]["cc-jsonl-aaa"]]
    assert stamps == sorted(stamps)


# ── _narrative_context ──

def test_narrative_context_keeps_intentional_drops_auto():
    mems = [
        _batch("cc", "cc-jsonl-aaa", 1),  # auto — excluded
        {"agent_id": "cc", "session_id": "cc-2026-07-01-10-00-00",
         "timestamp": "2026-07-01T10:00:00+00:00",
         "summary": "shipped v4.6.0 with zero failures", "key_facts": []},
        {"agent_id": "rocky", "session_id": "rocky-2026-07-01-10-00-00",
         "timestamp": "2026-07-01T10:00:00+00:00",
         "summary": "rocky's save, wrong agent", "key_facts": []},
    ]
    ctx = dream._narrative_context(mems, "cc")
    assert "shipped v4.6.0" in ctx
    assert "auto-sync from JSONL" not in ctx
    assert "wrong agent" not in ctx


# ── _build_session_section ──

def test_build_session_section_includes_narrative_block():
    batches = [_batch("cc", "cc-jsonl-aaa", i) for i in range(3)]
    section = dream._build_session_section("cc", "cc-jsonl-aaa", batches, "the lesson")
    assert "session=cc-jsonl-aaa" in section
    assert "NARRATIVE CONTEXT" in section
    assert "the lesson" in section
    # no narrative → no empty header
    section2 = dream._build_session_section("cc", "cc-jsonl-aaa", batches, "")
    assert "NARRATIVE CONTEXT" not in section2


def test_build_session_section_caps_recency_first(monkeypatch):
    monkeypatch.setattr(dream, "STRATEGY_SESSION_MAX_CHARS", 400)
    batches = [_batch("cc", "cc-jsonl-aaa", i, summary=f"batch-{i} " + "x" * 120)
               for i in range(6)]
    section = dream._build_session_section("cc", "cc-jsonl-aaa", batches, "")
    assert "batch-5" in section          # newest kept
    assert "batch-0" not in section      # oldest dropped
    assert "omitted" in section          # drop announced, never silent


# ── _validate_strategy_items ──

def _item(**overrides) -> dict:
    base = {
        "task_type": "live-store-migration",
        "task_description": "re-embed a live vector store without mixing spaces",
        "steps": [{"action": "pause capture", "tool_used": "mnemo_capture_pause",
                   "result_summary": "no writes during migration"}],
        "outcome": "zero-loss reindex; pause capture before touching the store",
        "rating": 5,
        "derived_from": "success",
        "evidence": "capture PAUSED for ~40 min",
    }
    base.update(overrides)
    return base


def test_validate_accepts_good_item_and_composes_body():
    out = dream._validate_strategy_items([_item()], "cc", "cc-jsonl-aaa")
    assert len(out) == 1
    body = out[0]
    assert body["agent_id"] == "cc"
    assert body["source"] == "dreamer"
    assert body["derived_from"] == "success"
    assert body["evidence_source"].startswith("dream:")
    assert "cc-jsonl-aaa" in body["evidence_source"]
    assert "capture PAUSED" in body["evidence_source"]
    assert len(body["evidence_source"]) <= 500
    assert body["steps"][0]["tool_used"] == "mnemo_capture_pause"


def test_validate_drops_malformed_items():
    bad = [
        _item(derived_from="maybe"),          # invalid enum
        _item(steps=[]),                      # no steps
        _item(task_type=""),                  # no task_type
        _item(outcome=None),                  # no outcome
        "not a dict",
        _item(steps=[{"tool_used": "bash"}]),  # steps without an action
    ]
    assert dream._validate_strategy_items(bad, "cc", "sid") == []


def test_validate_normalizes_string_steps_and_clamps_rating():
    out = dream._validate_strategy_items(
        [_item(steps=["do the thing", ""], rating=99)], "cc", "sid")
    assert len(out) == 1
    assert out[0]["steps"] == [
        {"action": "do the thing", "tool_used": None, "result_summary": ""}]
    assert out[0]["rating"] == 5
    out2 = dream._validate_strategy_items([_item(rating="not-a-number")], "cc", "sid")
    assert out2[0]["rating"] == 3


# ── flag_stale_trajectories ──

def _write_traj(tdir: Path, *, traj_id: str, created_days_ago: float,
                task_type: str = "bus_debug") -> None:
    tdir.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": traj_id, "agent_id": "cc", "task_type": task_type,
        "task_description": "fix the bus path", "steps": [{"action": "a"}],
        "outcome": "o", "rating": 5, "source": "agent",
        "created_at": time.time() - created_days_ago * 86400.0,
    }
    with (tdir / f"{task_type}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def test_flags_old_never_recalled_trajectory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dream, "AGENTS_ROOT", tmp_path)
    _write_traj(tmp_path / "cc" / "trajectories", traj_id="old1", created_days_ago=120)
    flags = dream.flag_stale_trajectories()
    assert len(flags) == 1
    assert "cc/bus_debug" in flags[0]
    assert "recalls=0" in flags[0]


def test_fresh_or_recently_recalled_not_flagged(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dream, "AGENTS_ROOT", tmp_path)
    tdir = tmp_path / "cc" / "trajectories"
    _write_traj(tdir, traj_id="fresh", created_days_ago=5)
    _write_traj(tdir, traj_id="old-but-used", created_days_ago=120,
                task_type="shopify_fix")
    (tdir / "recall_stats.json").write_text(json.dumps(
        {"old-but-used": {"recall_count": 3, "last_recalled": time.time() - 86400}}))
    assert dream.flag_stale_trajectories() == []


def test_flag_pass_survives_corrupt_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dream, "AGENTS_ROOT", tmp_path)
    tdir = tmp_path / "cc" / "trajectories"
    _write_traj(tdir, traj_id="old1", created_days_ago=120)
    with (tdir / "bus_debug.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"torn line\n')
    (tdir / "recall_stats.json").write_text("{not json")
    flags = dream.flag_stale_trajectories()
    assert len(flags) == 1  # torn line + corrupt stats skipped, real flag kept


# ── the gate ──

def test_strategies_gate_defaults_off(monkeypatch):
    """MNEMO_DREAM_STRATEGIES unset → gate closed (opt-in, public-safe)."""
    monkeypatch.delenv("MNEMO_DREAM_STRATEGIES", raising=False)
    spec = importlib.util.spec_from_file_location("mnemo_dream_gate", _DREAM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.MNEMO_DREAM_STRATEGIES is False
