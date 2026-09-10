"""The lexical lane on the wire (v4.21): the server-side block in /context.

test_vec.py pins lexical_terms/lexical_search and test_ranking.py the
composite term; the harnesses pin the outcome. This file pins the join
itself — a memory OUTSIDE the kNN's returned set reaches the caller as a
LEX chunk — and the two failure paths the block must degrade through.
"""
from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig, RankingConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.vec import EMBED_DIM, VecStore

_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


def _axis(i: int, sign: float = 1.0) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[i] = sign
    return v


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"): return _axis(0)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "decision"
    async def health_check(self): return True


def _client(tmp_path, ranking=None):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50098),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        ranking=ranking or RankingConfig(), personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        return TestClient(create_app(cfg))


def _seed(tmp_path, memory_id, summary, vec):
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    ts = time.time() - 86400
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": summary, "key_facts": [],
        "category": "decision", "source": "user", "created_at": ts,
    }))
    store = VecStore(base / "vec_index.sqlite")
    store.upsert(memory_id, summary, vec, source_file=(mem_dir / f"{memory_id}.json").as_posix(),
                 created_at=ts, category="decision")
    store.close()


def _seed_world(tmp_path):
    """Query = axis 0. `near` sits ON the query; seven orthogonal fillers;
    `target` is the antipode (cosine -1), guaranteed outside a kNN of 7 —
    which is the overfetch for max_results=2 (max(2*3, 2+5) = 7)."""
    _seed(tmp_path, "near", "the memory server listens on the workstation", _axis(0))
    for i in range(1, 8):
        _seed(tmp_path, f"filler-{i}", f"filler memory number {i} about nothing in particular", _axis(i))
    _seed(tmp_path, "target", "advisory GHSA-7q2x-4mvp-9rrk accepted: branch unreachable", _axis(0, -1.0))


def _context(client, prompt, **kw):
    r = client.post("/context", json={"prompt": prompt, "max_results": 2, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_memory_outside_the_knn_reaches_the_caller_as_a_lex_chunk(tmp_path):
    _seed_world(tmp_path)
    with _client(tmp_path) as client:
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    served = {c["memory_id"]: c for c in body["chunks"]}
    assert "target" in served, body
    assert served["target"]["cache_tier"] == "LEX"
    assert served["near"]["cache_tier"] == "VEC"           # meaning still wins slot 1
    assert body["chunks"][0]["memory_id"] == "near"
    assert body["cache_hits"]["LEX"] == 1 and body["cache_hits"]["VEC"] == 1
    # the LEX chunk's relevance is on VEC's wire scale: cosine -1 → d = 2 → 1/3
    assert served["target"]["relevance"] == pytest.approx(1 / 3, abs=1e-3)


def test_lane_off_is_pure_vector_recall(tmp_path):
    _seed_world(tmp_path)
    with _client(tmp_path, RankingConfig(lexical_enabled=False)) as client:
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert "target" not in {c["memory_id"] for c in body["chunks"]}
    assert body["cache_hits"]["LEX"] == 0
    assert all(c["cache_tier"] == "VEC" for c in body["chunks"])


def test_lexical_row_without_a_vector_is_skipped(tmp_path):
    """A vec_lex row whose vector is gone (a torn upsert, a hand edit) is
    not a served memory: the lane must skip it, not raise or fabricate."""
    _seed_world(tmp_path)
    store = VecStore(tmp_path / "agents" / "default" / "vec_index.sqlite")  # vec0 needs the extension loaded
    with store._conn:
        store._conn.execute("DELETE FROM vec_embeddings WHERE memory_id = 'target'")
    store.close()
    with _client(tmp_path) as client:
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert "target" not in {c["memory_id"] for c in body["chunks"]}
    assert body["cache_hits"]["LEX"] == 0


def test_a_broken_lexical_table_degrades_to_vector_recall_out_loud(tmp_path, caplog):
    _seed_world(tmp_path)
    with _client(tmp_path) as client, \
         patch.object(VecStore, "lexical_search", side_effect=sqlite3.OperationalError("no such module: fts5")):
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert body["chunks"] and all(c["cache_tier"] == "VEC" for c in body["chunks"])
    assert "lexical lane failed" in caplog.text


def test_dimension_mismatch_keeps_the_lane_out_of_the_pool(tmp_path):
    """The kNN screams and serves nothing on a query of the wrong dimension
    so L3 can rescue the recall; the lane must not fill the pool with
    cosines over a truncating zip."""
    _seed_world(tmp_path)

    class ShortEmbedding(FakeEmbedding):
        async def embed(self, text, *, use_breaker=True, task_type="document"):
            return [1.0] * (EMBED_DIM // 2)

    with patch("agentb.server.create_resilient_embedding", return_value=ShortEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        cfg = AgentBConfig(
            reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
            embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
            cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50098),
            data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
            personas=dict(DEFAULT_PERSONAS),
        )
        with TestClient(create_app(cfg)) as client:
            body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert body["cache_hits"]["LEX"] == 0
    assert all(c["cache_tier"] != "LEX" for c in body["chunks"])
