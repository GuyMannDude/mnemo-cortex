"""A demoted memory stays out of the index, and a save the embedder missed is
indexed later (order #6658 item 9).

9a: `backfill` (and the reindex) used to re-embed records carrying
`superseded_by`, so one `POST /vec/backfill` undid every demotion in a tenant.
9b: a save whose embedding failed was kept raw and never indexed again; now
its id goes on the tenant's pending list, which drains on the next good save
and at startup.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, AgentConfig, CacheConfig, ClassificationConfig,
    DEFAULT_PERSONAS, ProviderConfig, ResilientProviderConfig, ServerConfig,
)
from agentb.migrate import run_reindex
from agentb.vec import EMBED_DIM, PendingIndex, VecStore, backfill, drain_pending

VEC = [0.0] * EMBED_DIM
VEC[0] = 1.0


def _write(mem: Path, mid: str, summary: str, **extra) -> None:
    mem.mkdir(parents=True, exist_ok=True)
    (mem / f"{mid}.json").write_text(json.dumps(
        {"id": mid, "summary": summary, "created_at": time.time(), **extra}))


class Embed:
    def __init__(self):
        self.calls: list[str] = []
        self.down = False

    async def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.down:
            raise RuntimeError("All connection attempts failed")
        return list(VEC)


# ── 9a: backfill + reindex leave demoted memories out ──

@pytest.mark.asyncio
async def test_backfill_does_not_index_a_superseded_record(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    _write(mem, "live", "the vault hosts mnemo")
    _write(mem, "dead", "the vault hosts nothing", superseded_by="demoted:cc:1")
    embed = Embed()
    stats = await backfill(store, mem, embed, adaptive=False)
    assert stats["embedded"] == 1
    assert store.has("live")
    assert not store.has("dead")
    assert embed.calls == ["the vault hosts mnemo"]


@pytest.mark.asyncio
async def test_backfill_removes_a_superseded_record_an_earlier_run_revived(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    _write(mem, "dead", "old answer", superseded_by="newer")
    store.upsert("dead", "old answer", list(VEC))
    stats = await backfill(store, mem, Embed(), adaptive=False)
    assert stats["superseded_indexed"] == 1
    assert stats["superseded_removed"] == 1
    assert not store.has("dead")


@pytest.mark.asyncio
async def test_backfill_dry_run_counts_and_writes_nothing(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    _write(mem, "a", "unindexed one")
    _write(mem, "b", "unindexed two")
    _write(mem, "c", "already indexed")
    _write(mem, "d", "revived", superseded_by="demoted:cc:1")
    store.upsert("c", "already indexed", list(VEC))
    store.upsert("d", "revived", list(VEC))
    embed = Embed()
    stats = await backfill(store, mem, embed, dry_run=True)
    assert stats["dry_run"] is True
    assert stats["unindexed"] == 2
    assert stats["superseded_indexed"] == 1
    assert stats["embedded"] == 0 and stats["superseded_removed"] == 0
    assert embed.calls == []
    assert store.count() == 2 and store.has("d")


@pytest.mark.asyncio
async def test_reindex_skips_and_removes_superseded(tmp_path):
    mem = tmp_path / "agents" / "cc" / "memory"
    _write(mem, "live", "memory one")
    _write(mem, "dead", "memory two", superseded_by="live")
    store = VecStore(tmp_path / "agents" / "cc" / "vec_index.sqlite")
    store.upsert("dead", "memory two", list(VEC))
    store.close()
    cfg = AgentBConfig()
    cfg.data_dir = str(tmp_path)
    embed = Embed()
    res = await run_reindex(["cc"], dry_run=False, backup=False,
                            include_trajectories=False, config=cfg, embed=embed)
    assert res[0]["reembedded"] == 1
    assert res[0]["superseded_removed"] == 1
    assert embed.calls == ["memory one"]
    store = VecStore(tmp_path / "agents" / "cc" / "vec_index.sqlite")
    try:
        assert store.has("live") and not store.has("dead")
    finally:
        store.close()


# ── 9b: the pending list ──

@pytest.mark.asyncio
async def test_drain_indexes_pending_and_keeps_the_rest_while_down(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    pending = PendingIndex(tmp_path / "vec_pending.txt")
    for mid in ("a", "b"):
        _write(mem, mid, f"saved while down {mid}")
        pending.add(mid)
    embed = Embed()
    embed.down = True
    stats = await drain_pending(store, mem, pending, embed)
    assert stats["embedded"] == 0 and stats["left"] == 2
    assert len(embed.calls) == 1  # stops at the first failure, no hammering
    assert pending.ids() == ["a", "b"]

    embed.down = False
    stats = await drain_pending(store, mem, pending, embed)
    assert stats["embedded"] == 2 and stats["left"] == 0
    assert store.has("a") and store.has("b")
    assert not pending.path.exists()


@pytest.mark.asyncio
async def test_drain_drops_superseded_missing_and_already_indexed(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    pending = PendingIndex(tmp_path / "vec_pending.txt")
    _write(mem, "dead", "demoted since", superseded_by="demoted:cc:1")
    _write(mem, "done", "a backfill got it first")
    store.upsert("done", "a backfill got it first", list(VEC))
    for mid in ("dead", "gone", "done"):
        pending.add(mid)
    embed = Embed()
    stats = await drain_pending(store, mem, pending, embed)
    assert embed.calls == []
    assert stats["left"] == 0
    assert not store.has("dead")
    assert not pending.path.exists()


@pytest.mark.asyncio
async def test_drain_keeps_ids_added_while_it_ran(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    pending = PendingIndex(tmp_path / "vec_pending.txt")
    _write(mem, "a", "first")
    _write(mem, "late", "saved during the drain")
    pending.add("a")

    class AddsMidway(Embed):
        async def __call__(self, text):
            pending.add("late")
            return await super().__call__(text)

    await drain_pending(store, mem, pending, AddsMidway())
    assert store.has("a")
    assert pending.ids() == ["late"]


# ── 9b through the server ──

STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
          "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class SwitchEmbedding:
    """Server-side fake: `down` makes every document embed fail."""
    active_label = "fake/embed"
    status = STATUS

    def __init__(self):
        self.down = False

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        if self.down and task_type == "document":
            raise RuntimeError("All connection attempts failed")
        return list(VEC)

    async def health_check(self):
        return True


class FakeReasoning:
    active_label = "fake/reason"
    status = STATUS

    async def generate(self, *args, **kwargs):
        return "doctrine"

    async def health_check(self):
        return True


def _cfg(tmp_path: Path) -> AgentBConfig:
    return AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        cache=CacheConfig(), classification=ClassificationConfig(enabled=False),
        server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path / "default"), personas=dict(DEFAULT_PERSONAS),
        agents={"cody": AgentConfig(data_dir=str(tmp_path / "cody"))},
    )


def _client(tmp_path: Path, emb: SwitchEmbedding):
    from agentb.server import create_app
    p1 = patch("agentb.server.create_resilient_embedding", return_value=emb)
    p2 = patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning())
    p1.start(), p2.start()
    try:
        return TestClient(create_app(_cfg(tmp_path)))
    finally:
        p1.stop(), p2.stop()


def _save(client, session, text):
    r = client.post("/writeback", json={
        "session_id": session, "summary": text, "agent_id": "cody",
        "category": "doctrine", "source": "inferred", "force": True})
    assert r.status_code == 200, r.text
    return r.json()["memory_id"]


def _wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_save_while_embedder_down_is_indexed_on_the_next_good_save(tmp_path):
    emb = SwitchEmbedding()
    pending_file = tmp_path / "cody" / "vec_pending.txt"
    with _client(tmp_path, emb) as client:
        emb.down = True
        missed = _save(client, "s1", "Ollama blipped while this was saved")
        assert pending_file.read_text().split() == [missed]
        emb.down = False
        _save(client, "s2", "a later save with the embedder back")
        assert _wait_for(lambda: not pending_file.exists())
        store = client.app.state.tenants.get("cody")["vec"]
        assert store.has(missed)


def test_pending_list_drains_at_startup(tmp_path):
    emb = SwitchEmbedding()
    pending_file = tmp_path / "cody" / "vec_pending.txt"
    _write(tmp_path / "cody" / "memory", "m1", "saved raw before the restart")
    pending_file.write_text("m1\n")
    with _client(tmp_path, emb) as client:
        assert _wait_for(lambda: not pending_file.exists())
        assert client.app.state.tenants.get("cody")["vec"].has("m1")


def test_vec_backfill_dry_run_endpoint(tmp_path):
    emb = SwitchEmbedding()
    with _client(tmp_path, emb) as client:
        _write(tmp_path / "cody" / "memory", "raw1", "kept raw, never indexed")
        r = client.post("/vec/backfill", params={"agent_id": "cody", "dry_run": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True and body["unindexed"] == 1
        assert not client.app.state.tenants.get("cody")["vec"].has("raw1")
