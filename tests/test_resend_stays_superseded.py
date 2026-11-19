"""A re-send of a demoted memory leaves it demoted (item 16).

/writeback derives the memory id from session_id + timestamp + text, so an
identical re-send (an import retried, the dreamer's 13b re-send, a backfill
that reuses one timestamp) maps to the same id. Before: that re-send
overwrote the demoted record with a fresh one, sealed it again and put its
vector back, and /context served it. Now nothing is written, sealed or
indexed; the answer is 200, status "superseded".

The Analyst has the same shape (id = lens + agent + summary): a note it
derives again after a demote is skipped and counted, not rewritten.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agentb.analyst import analyze_tenant
from agentb.config import (
    AgentBConfig, AgentConfig, AnalysisConfig, CacheConfig, ClassificationConfig,
    DEFAULT_PERSONAS, ProviderConfig, ResilientProviderConfig, ServerConfig,
)
from agentb.ledger import get_ledger
from agentb.vec import EMBED_DIM, VecStore

VEC = [0.0] * EMBED_DIM
VEC[0] = 1.0
STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
          "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}
STAMP = "2026-11-01T10:00:00+00:00"


class FakeEmbedding:
    active_label = "fake/embed"
    status = STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)

    async def health_check(self):
        return True


class FakeReasoning:
    active_label = "fake/reason"
    status = STATUS

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return "[]"

    async def health_check(self):
        return True


def _client(tmp_path: Path) -> TestClient:
    from agentb.server import create_app
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        cache=CacheConfig(), classification=ClassificationConfig(enabled=False),
        server=ServerConfig(host="127.0.0.1", port=50098),
        data_dir=str(tmp_path / "default"), personas=dict(DEFAULT_PERSONAS),
        agents={"cody": AgentConfig(data_dir=str(tmp_path / "cody"))},
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        return TestClient(create_app(cfg))


def _import(client, text: str, session: str = "import-1", **extra) -> dict:
    """An import-shaped save: fixed session id + timestamp, batch."""
    r = client.post("/writeback", json={
        "session_id": session, "timestamp": STAMP, "summary": text,
        "agent_id": "cody", "category": "doctrine", "source": "inferred",
        "batch": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _demote(client, memory_id: str) -> str:
    r = client.post("/memories/demote", json={
        "memory_id": memory_id, "agent_id": "cody", "by": "guy", "reason": "wrong"})
    assert r.status_code == 200 and r.json()["demoted"], r.text
    return r.json()["superseded_by"]


def _context(client, prompt: str) -> dict:
    r = client.post("/context", json={"prompt": prompt, "agent_id": "cody",
                                      "max_results": 10})
    assert r.status_code == 200, r.text
    return r.json()


def _seals(memory_dir: Path, memory_id: str) -> int:
    return sum(1 for e in get_ledger(memory_dir).entries()
               if e and e.get("memory_id") == memory_id)


def test_identical_resend_of_a_demoted_memory_stays_demoted(tmp_path):
    with _client(tmp_path) as client:
        memory = tmp_path / "cody" / "memory"
        vec = client.app.state.tenants.get("cody")["vec"]
        live = _import(client, "the vault lives on the-north-box", session="import-0")["memory_id"]
        first = _import(client, "the vault lives on the-old-box")
        assert first["status"] == "archived"
        mid = first["memory_id"]
        stamp = _demote(client, mid)
        record = memory / f"{mid}.json"
        after_demote = record.read_bytes()
        seals = _seals(memory, mid)

        again = _import(client, "the vault lives on the-old-box")

        assert again["status"] == "superseded"
        assert again["memory_id"] == mid
        assert again["superseded_by"] == stamp
        assert record.read_bytes() == after_demote      # nothing written
        assert _seals(memory, mid) == seals              # nothing sealed
        assert not vec.has(mid)                          # nothing indexed
        chunks = {c["memory_id"]: c for c in _context(client, "where does the vault live")["chunks"]}
        assert chunks[live]["cache_tier"] == "VEC"       # presence: the VEC lane served
        assert mid not in chunks


def test_resend_of_a_memory_superseded_by_a_later_save_names_the_newer_one(tmp_path):
    with _client(tmp_path) as client:
        vec = client.app.state.tenants.get("cody")["vec"]
        old = _import(client, "the printer is on shelf A")["memory_id"]
        new = _import(client, "the printer is on shelf B", session="import-2",
                      supersedes=[old])["memory_id"]
        again = _import(client, "the printer is on shelf A")
        assert again["status"] == "superseded"
        assert again["memory_id"] == old and again["superseded_by"] == new
        assert not vec.has(old) and vec.has(new)


def test_resend_of_a_live_memory_still_saves(tmp_path):
    """Control: only a superseded record is left alone."""
    with _client(tmp_path) as client:
        vec = client.app.state.tenants.get("cody")["vec"]
        mid = _import(client, "the drill bits are in drawer 3")["memory_id"]
        again = _import(client, "the drill bits are in drawer 3")
        assert again["status"] == "archived" and again["memory_id"] == mid
        assert again["superseded_by"] is None
        assert vec.has(mid)


# ── Analyst: the same note derived again after a demote ──

NOTE = {"category": "decision", "confidence": "high", "key_facts": ["blast radius"],
        "summary": "Chose a separate box for the shop app so it never shares Mnemo's host."}


class _Reasoner:
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return json.dumps([NOTE])


class _Embedder:
    def __init__(self):
        self.calls = 0

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        self.calls += 1
        return list(VEC)


def _seed_log(memory_dir: Path, mid: str) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{mid}.json").write_text(json.dumps({
        "id": mid, "session_id": f"cc-jsonl-{mid}", "summary": "we picked a separate box",
        "key_facts": [], "category": "session_log", "source": "tool",
        "created_at": time.time(), "timestamp": "2026-11-10T12:00:00"}))


def _pass(memory_dir: Path, store: VecStore) -> dict:
    return asyncio.run(analyze_tenant("cc", memory_dir, store, _Reasoner(), _Embedder(),
                                      config=AnalysisConfig()))


def test_analyst_does_not_rewrite_a_demoted_note(tmp_path):
    memory = tmp_path / "memory"
    store = VecStore(tmp_path / "vec.sqlite")
    _seed_log(memory, "log1")
    assert _pass(memory, store)["notes_saved"] == 1
    (note_path,) = [p for p in memory.glob("*.json") if p.stem != "log1"]
    note_id = note_path.stem
    # The demote, as /memories/demote does it: stamp the record, drop the vector.
    rec = json.loads(note_path.read_text())
    rec["superseded_by"] = "demoted:guy:1"
    note_path.write_text(json.dumps(rec))
    store.delete(note_id)
    after_demote = note_path.read_bytes()
    seals = _seals(memory, note_id)

    _seed_log(memory, "log2")  # a later log the lens reads to the same note
    stats = _pass(memory, store)

    assert stats["notes_superseded"] == 1
    assert stats["notes_saved"] == 0 and stats["failed"] == 0
    assert note_path.read_bytes() == after_demote
    assert _seals(memory, note_id) == seals
    assert not store.has(note_id)
    # Skipped is an answer: the source is marked, not retried forever.
    assert json.loads((memory / "log2.json").read_text())["analyst_processed"] is True
    store.close()
