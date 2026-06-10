"""Tests for the v4.0 reclassification migration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentb.config import AgentBConfig
from agentb.migrate import run_migration, _backup, _purge_empty, _category_spread
from tests.test_classify import FakeReasoner


def _mk_store(base: Path, agent: str, entries) -> Path:
    """entries: list of (id, category, summary, key_facts)."""
    md = base / "agents" / agent / "memory"
    md.mkdir(parents=True, exist_ok=True)
    for mid, cat, summary, facts in entries:
        (md / f"{mid}.json").write_text(json.dumps({
            "id": mid, "summary": summary, "key_facts": facts,
            "category": cat, "source": "tool", "created_at": 1.0,
            "schema_version": 3,
        }))
    return md


def _cfg(base: Path) -> AgentBConfig:
    c = AgentBConfig()
    c.data_dir = str(base)
    return c


def test_backup_snapshots_memory(tmp_path: Path):
    _mk_store(tmp_path, "cc", [("a", "unknown", "hello", [])])
    data_dir = tmp_path / "agents" / "cc"
    dest = _backup(data_dir)
    assert (dest / "memory" / "a.json").exists()


def test_category_spread_counts(tmp_path: Path):
    md = _mk_store(tmp_path, "cc", [
        ("a", "unknown", "x", []), ("b", "unknown", "y", []), ("c", "topology", "z", []),
    ])
    spread = _category_spread(md)
    assert spread == {"unknown": 2, "topology": 1}


def test_purge_empty_removes_only_sentinels(tmp_path: Path):
    md = _mk_store(tmp_path, "cc", [
        ("blank", "session_log", "", []),                      # sentinel → purge
        ("flush", "session_log", "x", ["auto_capture_flush"]),  # sentinel → purge
        ("real", "session_log", "A genuine session transcript worth keeping in Tier 2", []),  # keep
    ])
    data_dir = tmp_path / "agents" / "cc"
    removed = _purge_empty(data_dir)
    assert removed == 2
    assert not (md / "blank.json").exists()
    assert not (md / "flush.json").exists()
    assert (md / "real.json").exists()  # real session logs are the archive — never purged


@pytest.mark.asyncio
async def test_run_migration_dry_run_writes_nothing(tmp_path: Path):
    md = _mk_store(tmp_path, "cc", [("a", "unknown", "artforge on port 50001", [])])
    before = (md / "a.json").read_text()
    res = await run_migration(
        ["cc"], dry_run=True, backup=False, include_routine=True, purge_noise=False,
        config=_cfg(tmp_path), reasoner=FakeReasoner(reply="topology"),
    )
    assert res[0]["reclassified"] == 1
    assert (md / "a.json").read_text() == before


@pytest.mark.asyncio
async def test_run_migration_reclassifies_and_backs_up(tmp_path: Path):
    md = _mk_store(tmp_path, "cc", [("a", "unknown", "artforge on port 50001", [])])
    res = await run_migration(
        ["cc"], dry_run=False, backup=True, include_routine=True, purge_noise=False,
        config=_cfg(tmp_path), reasoner=FakeReasoner(reply="topology"),
    )
    assert res[0]["reclassified"] == 1
    assert json.loads((md / "a.json").read_text())["category"] == "topology"
    assert (tmp_path / "agents" / "cc" / ".migrate-backups").is_dir()
