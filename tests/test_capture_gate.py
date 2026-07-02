"""v4.1 capture pause gate — unit + endpoint behavior.

Contract: while paused, ambient capture (/ingest, auto-capture-shaped
/writeback) is DISCARDED; deliberate manual saves still land; the pause
auto-resumes at expiry without any background thread (lazy watchdog).
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.capture_gate import CaptureGate, MAX_PAUSE_MINUTES
from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)

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


# ── Unit ──

def test_gate_pause_resume_cycle(tmp_path):
    gate = CaptureGate(tmp_path)
    assert not gate.is_paused()
    st = gate.pause(minutes=5, reason="key rotation")
    assert st["paused"] and st["reason"] == "key rotation"
    assert gate.is_paused()
    st = gate.resume()
    assert not st["paused"]
    assert not gate.is_paused()


def test_gate_auto_resumes_after_expiry(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5)
    # Rewind the clock by editing the state file — no sleeping in tests.
    state = json.loads(gate.pause_file.read_text())
    state["resume_at"] = time.time() - 1
    gate.pause_file.write_text(json.dumps(state))
    assert not gate.is_paused()
    assert not gate.pause_file.exists(), "watchdog should clear the expired pause file"


def test_gate_clamps_to_max(tmp_path):
    gate = CaptureGate(tmp_path)
    st = gate.pause(minutes=10_000)
    remaining = st["remaining_seconds"]
    assert remaining <= MAX_PAUSE_MINUTES * 60


def test_gate_survives_restart(tmp_path):
    CaptureGate(tmp_path).pause(minutes=30)
    assert CaptureGate(tmp_path).is_paused()


def test_corrupt_pause_file_fails_open(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause_file.parent.mkdir(parents=True, exist_ok=True)
    gate.pause_file.write_text("{not json")
    assert not gate.is_paused()


# ── Endpoints ──

def test_pause_blocks_ingest_and_ambient_writeback_but_not_manual(client, tmp_path):
    r = client.post("/capture/pause", json={"minutes": 10, "reason": "test"})
    assert r.status_code == 200 and r.json()["paused"]

    # /ingest discarded
    r = client.post("/ingest", json={"prompt": "p", "response": "r"})
    assert r.status_code == 200 and r.json()["status"] == "paused"
    hot_dir = tmp_path / "agents" / "default" / "sessions" / "hot"
    assert not list(hot_dir.glob("*.jsonl")), "paused ingest must write nothing"

    # auto-capture-shaped writeback discarded (session_log category)
    r = client.post("/writeback", json={
        "session_id": "cc-jsonl-abc", "summary": "CC session activity (auto-sync from JSONL)",
        "source": "tool", "category": "session_log",
    })
    assert r.json()["status"] == "paused"
    assert r.json()["memory_id"] == ""

    # [AUTO-CAPTURE] shape discarded even without explicit category
    r = client.post("/writeback", json={
        "session_id": "cc-auto-1", "summary": "[AUTO-CAPTURE] 3 tool calls: ...",
    })
    assert r.json()["status"] == "paused"

    # deliberate manual save still lands
    r = client.post("/writeback", json={
        "session_id": "manual-1",
        "summary": "Chose Hetzner over DigitalOcean because of blast-radius isolation.",
        "category": "decision",
    })
    assert r.json()["status"] == "archived"
    assert r.json()["memory_id"]

    # status + health reflect the pause
    assert client.get("/capture/status").json()["paused"]
    assert client.get("/health").json()["capture"]["paused"]

    # resume restores capture
    client.post("/capture/resume")
    r = client.post("/ingest", json={"prompt": "p2", "response": "r2"})
    assert r.json()["status"] == "captured"
