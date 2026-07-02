"""v4.1 composite recall ranking — scoring unit tests + /context behavior.

The contract under test: similarity still dominates (an irrelevant memory
can never out-rank a strong match), but within the band of plausible matches
a doctrine beats a session log, fresh beats ancient, and frequently-recalled
beats never-recalled. Chunks with missing metadata get neutral values, never
penalties (pre-v3 records must stay accessible).
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.ranking import composite_score
from agentb.vec import VecStore
from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig, RankingConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)

CFG = RankingConfig()


def _score(sim=0.7, age=None, cat=None, access=0):
    return composite_score(similarity=sim, age_days=age, category=cat,
                           access_count=access, cfg=CFG)


# ── Unit: score ordering ──

def test_similarity_dominates():
    strong_log = _score(sim=0.9, cat="session_log")
    weak_doctrine = _score(sim=0.2, cat="doctrine")
    assert strong_log > weak_doctrine


def test_category_breaks_ties():
    assert _score(cat="doctrine") > _score(cat="topology") > _score(cat="session_log")


def test_recency_breaks_ties():
    assert _score(age=1) > _score(age=90)


def test_access_breaks_ties_and_saturates():
    assert _score(access=5) > _score(access=0)
    # saturating: 100 recalls worth barely more than 10
    assert _score(access=100) - _score(access=10) < 0.02


def test_missing_metadata_is_neutral_not_penalized():
    # unknown age must land between fresh and ancient, not below both
    assert _score(age=200) < _score(age=None) < _score(age=2)
    # uncategorized must beat session_log (it might be gold; a log is known noise)
    assert _score(cat=None) > _score(cat="session_log")


def test_weights_come_from_config():
    flat = RankingConfig(w_similarity=1.0, w_recency=0.0, w_importance=0.0, w_access=0.0)
    a = composite_score(similarity=0.5, age_days=1, category="doctrine",
                        access_count=50, cfg=flat)
    b = composite_score(similarity=0.5, age_days=900, category="session_log",
                        access_count=0, cfg=flat)
    assert a == pytest.approx(b)


# ── /context integration: doctrine out-ranks noise at lower similarity ──

VEC_A = [0.0] * 768
VEC_A[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"): return list(VEC_A)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "decision"
    async def health_check(self): return True


@pytest.fixture
def client(tmp_path):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(port=50099),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


def _seed_vec_memory(tmp_path, memory_id, summary, category, *, distance_vec, created_at=None):
    """Write a memory JSON + vec row directly. distance_vec controls how far the
    stored embedding sits from the query vector (VEC_A), i.e. raw similarity."""
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    ts = created_at or time.time()
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": summary, "key_facts": [],
        "category": category, "source": "user", "created_at": ts,
    }))
    store = VecStore(base / "vec_index.sqlite")
    store.upsert(memory_id, summary, distance_vec,
                 source_file=(mem_dir / f"{memory_id}.json").as_posix(), created_at=ts)
    store.close()


def test_context_ranks_doctrine_above_unknown_noise(tmp_path, client):
    # noise: slightly CLOSER to the query (higher raw similarity), category
    # unknown. The gap is the realistic tie-band from the quality audit —
    # composite ranking re-orders within that band; a LARGE similarity gap
    # would (correctly) still let the closer match win.
    near = list(VEC_A); near[1] = 0.2
    far = list(VEC_A); far[1] = 0.3
    _seed_vec_memory(tmp_path, "noise1", "uncategorized migration blob", "unknown",
                     distance_vec=near)
    _seed_vec_memory(tmp_path, "gold1", "DOCTRINE: brain files win over Mnemo on conflict",
                     "doctrine", distance_vec=far)

    r = client.post("/context", json={"prompt": "truth hierarchy", "max_results": 2})
    assert r.status_code == 200, r.text
    chunks = r.json()["chunks"]
    assert [c["memory_id"] for c in chunks][0] == "gold1", (
        "doctrine should out-rank unknown noise despite lower raw similarity")


def test_context_access_counts_persist_and_rise(tmp_path, client):
    _seed_vec_memory(tmp_path, "m1", "a decision about ports", "decision",
                     distance_vec=list(VEC_A))
    client.post("/context", json={"prompt": "ports", "max_results": 1})
    client.post("/context", json={"prompt": "ports", "max_results": 1})
    store = VecStore(tmp_path / "agents" / "default" / "vec_index.sqlite")
    counts = store.access_counts(["m1"])
    store.close()
    assert counts.get("m1", 0) >= 2


def test_context_exposes_memory_id(tmp_path, client):
    _seed_vec_memory(tmp_path, "mid1", "identity: Rocky is Hermes on IGOR", "identity",
                     distance_vec=list(VEC_A))
    r = client.post("/context", json={"prompt": "who is rocky", "max_results": 1})
    assert r.json()["chunks"][0]["memory_id"] == "mid1"
