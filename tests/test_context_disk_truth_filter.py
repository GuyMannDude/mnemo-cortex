"""v4.0.2 regression test: the L1 + L2 tiers must honor the category filter.

v4.0.1 fixed the VEC tier. But the category is canonical *on disk*
(`memory/<id>.json`), and the reclassification migration rewrote only those
files — not the L1/L2 tier caches. So:
  - L2 filtered against its stale cached category, and
  - L1 had no category (or memory_id) at all,
and `session_log` leaked past `/context` again. The fix re-reads disk-truth per
hit (`resolve_disk_truth`) before the filter, mirroring the VEC fix.

These tests isolate the L2 path from VEC (VEC is empty here, so L2 is the
enforcer), unit-test the L1 plumbing, and unit-test the helper directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.cache import L1Cache, ContextChunk, resolve_disk_truth

# Fixed 768-dim vector — fake provider embeds everything to this, so the query
# matches every stored memory and the metadata filter alone decides what returns.
VEC = [0.0] * 768
VEC[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"): return list(VEC)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "topology"
    async def health_check(self): return True


@pytest.fixture
def client(tmp_path):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50099),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


def _seed_l2_and_disk(tmp_path, memory_id, summary, disk_category, cached_category):
    """Seed a memory into L2's on-disk index (with a stale cached category) and
    its memory JSON (with the canonical/reclassified category), writing files
    directly so the request thread builds the tenant fresh. VEC is left empty on
    purpose, so L2 is the tier under test."""
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    l2_dir = base / "cache" / "l2"
    mem_dir.mkdir(parents=True, exist_ok=True)
    l2_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": summary, "key_facts": [],
        "category": disk_category, "source": "tool", "created_at": now,
    }))
    (l2_dir / "index.json").write_text(json.dumps([{
        "id": memory_id, "content": summary, "source": f"session:{memory_id}",
        "embedding": list(VEC), "created_at": now,
        "metadata": {"memory_id": memory_id, "provenance_source": "tool",
                     "category": cached_category},
    }]))


def test_l2_disk_truth_excludes_reclassified_session_log(client, tmp_path):
    # Cached as 'topology' (pre-migration) but reclassified to 'session_log' on disk.
    _seed_l2_and_disk(tmp_path, "m-stale", "raw auto-sync activity dump",
                      disk_category="session_log", cached_category="topology")

    # Default recall must exclude it on disk-truth, despite the stale L2 cache.
    r = client.post("/context", json={"prompt": "activity", "max_results": 5})
    assert r.status_code == 200, r.text
    cats = [c.get("category") for c in r.json()["chunks"]]
    assert "session_log" not in cats
    assert r.json()["cache_hits"]["L2"] == 0   # excluded, not served

    # Opt back in (exclude_categories=[]) → reachable via L2 (VEC is empty, so a
    # hit here proves the L2 path itself, not VEC, carried it).
    r2 = client.post("/context", json={"prompt": "activity", "max_results": 5,
                                       "exclude_categories": []})
    assert r2.status_code == 200, r2.text
    assert "session_log" in [c.get("category") for c in r2.json()["chunks"]]
    assert r2.json()["cache_hits"]["L2"] >= 1
    assert r2.json()["cache_hits"]["VEC"] == 0


def _seed_l1_and_disk(tmp_path, memory_id, content, disk_category, cached_category):
    """Seed an L1 bundle (with a stale cached category) and its memory JSON (with
    the canonical category) on disk. L2/VEC left empty so L1 is the tier under test."""
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    l1_dir = base / "cache" / "l1"
    mem_dir.mkdir(parents=True, exist_ok=True)
    l1_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": content, "key_facts": [],
        "category": disk_category, "source": "tool", "created_at": now,
    }))
    bid = hashlib.sha256(content.encode()).hexdigest()[:12]
    (l1_dir / f"{bid}.json").write_text(json.dumps({
        "id": bid, "content": content, "source": f"precache:{memory_id}",
        "embedding": list(VEC), "created_at": now,
        "memory_id": memory_id, "category": cached_category,
    }))


def test_l1_disk_truth_excludes_reclassified_session_log(client, tmp_path):
    # Bundle cached as 'topology' but the memory was reclassified to 'session_log' on disk.
    _seed_l1_and_disk(tmp_path, "m-l1", "raw auto-capture activity dump",
                      disk_category="session_log", cached_category="topology")

    r = client.post("/context", json={"prompt": "activity", "max_results": 5})
    assert r.status_code == 200, r.text
    assert "session_log" not in [c.get("category") for c in r.json()["chunks"]]
    assert r.json()["cache_hits"]["L1"] == 0   # excluded on disk-truth

    # Opt back in → reachable via L1 (VEC + L2 empty, so the hit proves the L1 path).
    r2 = client.post("/context", json={"prompt": "activity", "max_results": 5,
                                       "exclude_categories": []})
    assert r2.status_code == 200, r2.text
    assert "session_log" in [c.get("category") for c in r2.json()["chunks"]]
    assert r2.json()["cache_hits"]["L1"] >= 1
    assert r2.json()["cache_hits"]["VEC"] == 0
    assert r2.json()["cache_hits"]["L2"] == 0


def test_resolve_disk_truth_overrides_stale_chunk_category(tmp_path):
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "abc.json").write_text(json.dumps({
        "id": "abc", "category": "session_log", "source": "tool",
        "created_at": time.time(),
    }))
    # Chunk carries the stale category the migration left behind.
    chunk = ContextChunk("x", "l2-memory", 0.9, "L2",
                         memory_id="abc", category="topology", provenance_source=None)
    out = resolve_disk_truth(chunk, mem_dir)
    assert out.category == "session_log"   # disk wins
    assert out.provenance_source == "tool"

    # No memory_id (legacy entry), clean content → untouched, never raises.
    bare = ContextChunk("y", "l1-cache", 0.9, "L1")
    assert resolve_disk_truth(bare, mem_dir).category is None
    # No memory_id but auto-capture-shaped content → tagged session_log so the
    # default two-tier hiding applies to legacy cache entries too (v4.1).
    noisy = ContextChunk("[AUTO-CAPTURE] 3 tool calls: ...", "l2-memory", 0.9, "L2")
    assert resolve_disk_truth(noisy, mem_dir).category == "session_log"
    # memory_id with NO file on disk = deleted memory → dropped (v4.1).
    # The old no-op here is how purged [AUTO-CAPTURE] rows kept resurfacing
    # through L2 after the June-9 dedup sweep.
    ghost = ContextChunk("z", "l2-memory", 0.9, "L2", memory_id="nope")
    assert resolve_disk_truth(ghost, mem_dir) is None


def test_l1_add_search_round_trips_memory_id_and_category(tmp_path):
    l1 = L1Cache(tmp_path / "l1", CacheConfig())
    asyncio.run(
        l1.add("a bundle", "precache:m1", list(VEC), memory_id="m1", category="topology")
    )
    hits = l1.search(list(VEC), top_k=3)
    assert hits and hits[0].memory_id == "m1" and hits[0].category == "topology"
