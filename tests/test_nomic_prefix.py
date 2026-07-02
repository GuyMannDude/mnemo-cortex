"""Tests for the nomic task-prefix fix + `migrate reindex` (v4.6).

The prefix lives INSIDE the provider (on a local copy of the text), so:
  - only the API payload carries `search_query: ` / `search_document: `
  - callers' text, vec_sources.text, and truncation math stay un-prefixed
  - the Google fallback maps to its native taskType param, never a text prefix
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import agentb.migrate as migrate_mod
from agentb.config import AgentBConfig
from agentb.migrate import ReindexAbort, _embed_or_abort, run_reindex
from agentb.providers import (
    _NOMIC_PREFIX,
    GoogleEmbedding,
    OllamaEmbedding,
    ProviderConfig,
    ResilientProviderConfig,
    create_resilient_embedding,
)
from agentb.vec import EMBED_DIM, VecStore

VEC = [0.1] * EMBED_DIM


# ── payload capture ──

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _CapturingClient:
    """Stands in for httpx.AsyncClient; records the POSTed json payload."""
    captured: list[dict] = []
    response_payload: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        _CapturingClient.captured.append(kw.get("json", {}))
        return _FakeResponse(_CapturingClient.response_payload)


@pytest.fixture
def capture(monkeypatch):
    import httpx
    _CapturingClient.captured = []
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    return _CapturingClient


# ── unit: prefix per provider ──

class TestOllamaPrefix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("task_type,prefix", list(_NOMIC_PREFIX.items()))
    async def test_prefix_prepended_per_task_type(self, capture, task_type, prefix):
        capture.response_payload = {"embeddings": [VEC]}
        p = OllamaEmbedding(ProviderConfig(provider="ollama", model="nomic-embed-text",
                                           api_base="http://localhost:11434"))
        assert await p.embed("hello world", task_type=task_type) == VEC
        assert capture.captured[0]["input"] == f"{prefix}hello world"

    @pytest.mark.asyncio
    async def test_default_is_document(self, capture):
        capture.response_payload = {"embeddings": [VEC]}
        p = OllamaEmbedding(ProviderConfig(provider="ollama", model="nomic-embed-text",
                                           api_base="http://localhost:11434"))
        await p.embed("hello")
        assert capture.captured[0]["input"] == "search_document: hello"

    @pytest.mark.asyncio
    async def test_unknown_task_type_fails_loud(self, capture):
        p = OllamaEmbedding(ProviderConfig(provider="ollama", model="nomic-embed-text",
                                           api_base="http://localhost:11434"))
        with pytest.raises(KeyError):
            await p.embed("hello", task_type="clustering-typo")


class TestGoogleNoTextPrefix:
    @pytest.mark.asyncio
    async def test_query_maps_to_native_task_type(self, capture):
        capture.response_payload = {"embedding": {"values": VEC}}
        p = GoogleEmbedding(ProviderConfig(provider="google", model="gemini-embedding-001",
                                           api_key="k"))
        await p.embed("hello", task_type="query")
        payload = capture.captured[0]
        assert payload["content"]["parts"][0]["text"] == "hello"  # NO text prefix
        assert payload["taskType"] == "RETRIEVAL_QUERY"

    @pytest.mark.asyncio
    async def test_document_maps_to_native_task_type(self, capture):
        capture.response_payload = {"embedding": {"values": VEC}}
        p = GoogleEmbedding(ProviderConfig(provider="google", model="gemini-embedding-001",
                                           api_key="k"))
        await p.embed("hello", task_type="document")
        payload = capture.captured[0]
        assert payload["content"]["parts"][0]["text"] == "hello"
        assert payload["taskType"] == "RETRIEVAL_DOCUMENT"


class TestResilientThreading:
    def _resilient(self, with_fallback=False):
        fallbacks = [ProviderConfig(provider="openai", model="text-embedding-3-small",
                                    api_key="sk-test")] if with_fallback else []
        return create_resilient_embedding(ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="nomic",
                                   api_base="http://localhost:11434"),
            fallbacks=fallbacks,
        ))

    @pytest.mark.asyncio
    async def test_task_type_reaches_primary(self):
        r = self._resilient()
        r.primary.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        await r.embed("q", task_type="query")
        assert r.primary.embed.call_args.kwargs["task_type"] == "query"

    @pytest.mark.asyncio
    async def test_default_task_type_is_document(self):
        r = self._resilient()
        r.primary.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        await r.embed("d")
        assert r.primary.embed.call_args.kwargs["task_type"] == "document"

    @pytest.mark.asyncio
    async def test_task_type_reaches_fallback(self):
        r = self._resilient(with_fallback=True)
        r._locked_dim = 3
        r.primary.embed = AsyncMock(side_effect=Exception("ollama down"))
        r.fallbacks[0].embed = AsyncMock(return_value=[0.4, 0.5, 0.6])
        await r.embed("q", task_type="query")
        assert r.fallbacks[0].embed.call_args.kwargs["task_type"] == "query"


# ── migration: seeded temp store ──

def _mk_store(base: Path, agent: str, entries) -> Path:
    md = base / "agents" / agent / "memory"
    md.mkdir(parents=True, exist_ok=True)
    for mid, summary in entries:
        (md / f"{mid}.json").write_text(json.dumps({
            "id": mid, "summary": summary, "key_facts": [], "category": "decision",
            "source": "tool", "created_at": 1.0, "schema_version": 3,
        }))
    return md


def _mk_trajectory(base: Path, agent: str, traj_id: str) -> Path:
    td = base / "agents" / agent / "trajectories"
    td.mkdir(parents=True, exist_ok=True)
    (td / "deploy.jsonl").write_text(json.dumps({
        "id": traj_id, "agent_id": agent, "task_type": "deploy",
        "task_description": "ship the fix", "steps": [{"action": "run tests"}],
        "outcome": "green", "rating": 5, "timestamp": "2026-07-01T00:00:00Z",
        "created_at": 2.0,
    }) + "\n")
    return td


def _mk_caches(base: Path, agent: str):
    l1 = base / "agents" / agent / "cache" / "l1"
    l2 = base / "agents" / agent / "cache" / "l2"
    l1.mkdir(parents=True, exist_ok=True)
    l2.mkdir(parents=True, exist_ok=True)
    (l1 / "aaa.json").write_text("{}")
    (l2 / "index.json").write_text("[]")


def _cfg(base: Path) -> AgentBConfig:
    c = AgentBConfig()
    c.data_dir = str(base)
    return c


class _FakeEmbed:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(VEC)


@pytest.fixture
def fake_embed():
    return _FakeEmbed()


@pytest.mark.asyncio
async def test_reindex_reembeds_and_stores_unprefixed_text(tmp_path: Path, fake_embed):
    _mk_store(tmp_path, "cc", [("a", "the vault hosts mnemo"), ("b", "rocky runs hermes")])
    res = await run_reindex(["cc"], dry_run=False, backup=False,
                            include_trajectories=True, config=_cfg(tmp_path),
                            embed=fake_embed)
    assert res[0]["reembedded"] == 2
    # the embed input and the stored text are both the RAW text — the prefix
    # is provider-side only and must never leak into vec_sources
    assert all(not t.startswith("search_document: ") for t in fake_embed.calls)
    store = VecStore(tmp_path / "agents" / "cc" / "vec_index.sqlite")
    try:
        assert store.has("a") and store.has("b")
        import sqlite3
        rows = store._conn.execute("SELECT text FROM vec_sources").fetchall()
        assert rows and all("search_document: " not in r[0] for r in rows)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reindex_covers_trajectories(tmp_path: Path, fake_embed):
    _mk_store(tmp_path, "cc", [("a", "memory one")])
    _mk_trajectory(tmp_path, "cc", "traj1")
    res = await run_reindex(["cc"], dry_run=False, backup=False,
                            include_trajectories=True, config=_cfg(tmp_path),
                            embed=fake_embed)
    assert res[0]["traj_reembedded"] == 1
    tstore = VecStore(tmp_path / "agents" / "cc" / "trajectories" / "traj_index.sqlite")
    try:
        assert tstore.has("traj1")
    finally:
        tstore.close()


@pytest.mark.asyncio
async def test_reindex_wipes_l1_l2_caches(tmp_path: Path, fake_embed):
    _mk_store(tmp_path, "cc", [("a", "memory one")])
    _mk_caches(tmp_path, "cc")
    res = await run_reindex(["cc"], dry_run=False, backup=False,
                            include_trajectories=False, config=_cfg(tmp_path),
                            embed=fake_embed)
    assert res[0]["cache_files_wiped"] == 2
    assert not (tmp_path / "agents" / "cc" / "cache" / "l1" / "aaa.json").exists()
    assert not (tmp_path / "agents" / "cc" / "cache" / "l2" / "index.json").exists()


@pytest.mark.asyncio
async def test_reindex_backs_up_including_trajectories(tmp_path: Path, fake_embed):
    _mk_store(tmp_path, "cc", [("a", "memory one")])
    _mk_trajectory(tmp_path, "cc", "traj1")
    await run_reindex(["cc"], dry_run=False, backup=True,
                      include_trajectories=True, config=_cfg(tmp_path),
                      embed=fake_embed)
    backups = list((tmp_path / "agents" / "cc" / ".migrate-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "memory" / "a.json").exists()
    assert (backups[0] / "trajectories" / "deploy.jsonl").exists()


@pytest.mark.asyncio
async def test_reindex_is_idempotent(tmp_path: Path, fake_embed):
    _mk_store(tmp_path, "cc", [("a", "memory one")])
    first = await run_reindex(["cc"], dry_run=False, backup=False,
                              include_trajectories=False, config=_cfg(tmp_path),
                              embed=fake_embed)
    second = await run_reindex(["cc"], dry_run=False, backup=False,
                               include_trajectories=False, config=_cfg(tmp_path),
                               embed=fake_embed)
    assert first[0]["reembedded"] == second[0]["reembedded"] == 1
    store = VecStore(tmp_path / "agents" / "cc" / "vec_index.sqlite")
    try:
        assert store._conn.execute("SELECT COUNT(*) FROM vec_sources").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(tmp_path: Path, fake_embed):
    _mk_store(tmp_path, "cc", [("a", "memory one")])
    _mk_caches(tmp_path, "cc")
    res = await run_reindex(["cc"], dry_run=True, backup=True,
                            include_trajectories=True, config=_cfg(tmp_path),
                            embed=fake_embed)
    assert res[0]["memories"] == 1
    assert fake_embed.calls == []
    assert not (tmp_path / "agents" / "cc" / "vec_index.sqlite").exists()
    assert not (tmp_path / "agents" / "cc" / ".migrate-backups").exists()
    assert (tmp_path / "agents" / "cc" / "cache" / "l1" / "aaa.json").exists()


@pytest.mark.asyncio
async def test_persistent_embed_failure_aborts(monkeypatch):
    async def _no_sleep(_):
        pass
    monkeypatch.setattr(migrate_mod.asyncio, "sleep", _no_sleep)

    async def _down(text):
        raise ConnectionError("ollama down")

    with pytest.raises(ReindexAbort):
        await _embed_or_abort(_down, "some text")
