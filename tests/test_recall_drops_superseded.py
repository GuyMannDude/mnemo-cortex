"""A demoted memory never surfaces, whatever put its vector back
(order #6658 item 9d).

Item 9 stopped backfill from re-embedding records that carry `superseded_by`,
but /context and /preflight still trusted the index: a demoted memory whose
vector was back in it (an older backfill, a hand repair) was served as live.
Now every lane that reads the record drops it and logs the id once.

Also here: drain_pending moves an id that just failed to the tail of the
pending list, so one record that always fails cannot hold up the rest.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, AgentConfig, CacheConfig, ClassificationConfig,
    DEFAULT_PERSONAS, ProviderConfig, ResilientProviderConfig, ServerConfig,
)
from agentb.vec import EMBED_DIM, PendingIndex, VecStore, drain_pending

VEC = [0.0] * EMBED_DIM
VEC[0] = 1.0
STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
          "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    status = STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)

    async def health_check(self):
        return True


class RecordingReasoning:
    """Answers /preflight with a valid verdict and keeps every prompt."""
    active_label = "fake/reason"
    status = STATUS

    def __init__(self):
        self.prompts: list[str] = []

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        self.prompts.append(prompt)
        return '{"verdict": "PASS", "confidence": 0.9, "reason": "ok"}'

    async def health_check(self):
        return True


def _client(tmp_path: Path, reasoner=None, embedder=None) -> TestClient:
    from agentb.server import create_app
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        cache=CacheConfig(), classification=ClassificationConfig(enabled=False),
        server=ServerConfig(host="127.0.0.1", port=50096),
        data_dir=str(tmp_path / "default"), personas=dict(DEFAULT_PERSONAS),
        agents={"cody": AgentConfig(data_dir=str(tmp_path / "cody"))},
    )
    with patch("agentb.server.create_resilient_embedding", return_value=embedder or FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning",
               return_value=reasoner or RecordingReasoning()):
        return TestClient(create_app(cfg))


def _save(client, text: str) -> str:
    r = client.post("/writeback", json={
        "session_id": "s", "summary": text, "agent_id": "cody",
        "category": "doctrine", "source": "inferred", "force": True})
    assert r.status_code == 200, r.text
    return r.json()["memory_id"]


def _damage(tmp_path: Path, client, memory_id: str) -> None:
    """Demote the record on disk and leave its vector in the index: the state
    an older backfill left behind."""
    path = tmp_path / "cody" / "memory" / f"{memory_id}.json"
    rec = json.loads(path.read_text())
    rec["superseded_by"] = "demoted:cc:1"
    path.write_text(json.dumps(rec))
    assert client.app.state.tenants.get("cody")["vec"].has(memory_id)


def _context(client, prompt: str) -> list[str]:
    r = client.post("/context", json={"prompt": prompt, "agent_id": "cody",
                                      "max_results": 10})
    assert r.status_code == 200, r.text
    return [c["memory_id"] for c in r.json()["chunks"]]


def _warnings(caplog, memory_id: str) -> int:
    return sum(1 for r in caplog.records
               if r.levelno == logging.WARNING and f"Demoted memory {memory_id}" in r.getMessage())


def test_context_vec_lane_drops_a_demoted_hit_and_warns_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agentb")
    with _client(tmp_path) as client:
        live = _save(client, "the vault lives on the-north-box")
        dead = _save(client, "the vault lives on the-old-box")
        _damage(tmp_path, client, dead)
        for _ in range(2):
            served = _context(client, "where does the vault live")
            assert live in served       # presence: the lane did serve
            assert dead not in served
    assert _warnings(caplog, dead) == 1


def test_context_lex_lane_drops_a_demoted_hit(tmp_path):
    with _client(tmp_path) as client:
        live = _save(client, "the hinge part is zq7731-live in the shop list")
        dead = _save(client, "the hinge part is zq7731-dead in the shop list")
        _damage(tmp_path, client, dead)
        store = client.app.state.tenants.get("cody")["vec"]
        # Blind the kNN so only the lexical lane can pool these memories.
        with patch.object(store, "search", return_value=[]), \
             patch.object(store, "newest", return_value=[]):
            served = _context(client, "which part is zq7731-live or zq7731-dead")
        assert live in served
        assert dead not in served


def test_preflight_never_shows_a_demoted_memory_to_the_reasoner(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agentb")
    reasoner = RecordingReasoning()
    with _client(tmp_path, reasoner) as client:
        _save(client, "the vault lives on the-north-box")
        dead = _save(client, "the vault lives on the-old-box")
        _damage(tmp_path, client, dead)
        r = client.post("/preflight", json={"prompt": "where is the vault",
                                            "draft_response": "the-north-box",
                                            "agent_id": "cody"})
        assert r.status_code == 200, r.text
    assert len(reasoner.prompts) == 1
    assert "the vault lives on the-north-box" in reasoner.prompts[0]
    assert "the-old-box" not in reasoner.prompts[0]
    assert _warnings(caplog, dead) == 1


class NearestIsDeadEmbedding(FakeEmbedding):
    """The query and the old-box memory get VEC exactly; every other memory sits
    at cosine ~0.9 (north-card further, at 0.8), so the demoted one is the
    nearest hit and the order behind it is fixed."""

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        if task_type == "query" or "old-box" in text:
            return list(VEC)
        v = [0.0] * EMBED_DIM
        v[0], v[1] = (0.8, 0.6) if "north-card" in text else (0.9, 0.4359)
        return v


def test_preflight_still_shows_two_live_memories_when_the_nearest_is_demoted(tmp_path):
    reasoner = RecordingReasoning()
    with _client(tmp_path, reasoner, NearestIsDeadEmbedding()) as client:
        _save(client, "the vault lives on the-north-box")
        _save(client, "the vault key hangs by the-north-door")
        _save(client, "the vault alarm code is on the-north-card")
        dead = _save(client, "the vault lives on the-old-box")
        _damage(tmp_path, client, dead)
        r = client.post("/preflight", json={"prompt": "where is the vault",
                                            "draft_response": "the-north-box",
                                            "agent_id": "cody"})
        assert r.status_code == 200, r.text
    assert len(reasoner.prompts) == 1
    memory_block = reasoner.prompts[0].split("USER'S PROMPT:")[0]   # not the draft
    assert "[VEC] the vault lives on the-north-box" in memory_block
    assert "[VEC] the vault key hangs by the-north-door" in memory_block
    assert "the-old-box" not in memory_block
    assert memory_block.count("[VEC] ") == 2              # still the two nearest live


def _write(mem: Path, mid: str, summary: str) -> None:
    mem.mkdir(parents=True, exist_ok=True)
    (mem / f"{mid}.json").write_text(json.dumps({"id": mid, "summary": summary}))


@pytest.mark.asyncio
async def test_drain_moves_a_record_that_always_fails_to_the_tail(tmp_path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    pending = PendingIndex(tmp_path / "vec_pending.txt")
    for mid in ("bad", "good"):
        _write(mem, mid, f"record {mid}")
        pending.add(mid)

    async def embed(text):
        if text == "record bad":
            raise ValueError("this record cannot be embedded")
        return list(VEC)

    stats = await drain_pending(store, mem, pending, embed)
    assert stats["failed"] == 1 and stats["embedded"] == 0
    assert pending.ids() == ["good", "bad"]

    stats = await drain_pending(store, mem, pending, embed)
    assert stats["embedded"] == 1 and stats["failed"] == 1
    assert store.has("good") and not store.has("bad")
    assert pending.ids() == ["bad"]
