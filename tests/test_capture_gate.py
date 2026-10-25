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
    state["pauses"]["*"]["resume_at"] = time.time() - 1  # v4.28 file shape: {"pauses": {scope: entry}}
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


# ── Per-agent scope (v4.28; snag: capture pause was server-wide, Opie #5185) ──

def test_scoped_pause_gates_only_that_agent(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, reason="cc rotating a key", agent_id="cc")
    assert gate.is_paused("cc")
    assert not gate.is_paused("opie"), "another agent's pause must not gate me"
    assert not gate.is_paused(None), "agent-less writes are not gated by cc's pause"
    st = gate.status()
    assert st["paused"] and not st["fleet_wide"]
    assert set(st["scopes"]) == {"cc"}


def test_fleet_wide_pause_gates_everyone(tmp_path):
    gate = CaptureGate(tmp_path)
    st = gate.pause(minutes=5, reason="all hands", agent_id=None)
    assert st["fleet_wide"]
    for who in ("cc", "opie", None):
        assert gate.is_paused(who)


def test_two_agents_hold_independent_pauses(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, agent_id="cc")
    gate.pause(minutes=5, agent_id="opie")
    assert gate.is_paused("cc") and gate.is_paused("opie") and not gate.is_paused("rocky")
    gate.resume(agent_id="cc")
    assert not gate.is_paused("cc")
    assert gate.is_paused("opie"), "resuming cc must leave opie's pause alone"


def test_scoped_resume_lifts_fleet_wide_too(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, agent_id=None)
    gate.resume(agent_id="rocky")
    assert not gate.is_paused("cc"), "a fleet-wide pause is owned by nobody; any resume lifts it"


def test_legacy_single_switch_file_reads_as_fleet_wide(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause_file.parent.mkdir(parents=True, exist_ok=True)
    gate.pause_file.write_text(json.dumps({
        "paused_at": time.time(), "resume_at": time.time() + 300,
        "minutes": 5, "reason": "set before upgrade",
    }))
    assert gate.is_paused("cc") and gate.status()["fleet_wide"]
    assert gate.status()["reason"] == "set before upgrade"


def test_expired_scope_drops_but_live_one_stays(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, agent_id="cc")
    gate.pause(minutes=5, agent_id="opie")
    state = json.loads(gate.pause_file.read_text())
    state["pauses"]["cc"]["resume_at"] = time.time() - 1
    gate.pause_file.write_text(json.dumps(state))
    assert not gate.is_paused("cc")
    assert gate.is_paused("opie")
    assert set(json.loads(gate.pause_file.read_text())["pauses"]) == {"opie"}


def test_endpoint_scoped_pause_spares_other_agents(client, tmp_path):
    r = client.post("/capture/pause", json={"minutes": 10, "reason": "t", "agent_id": "cc"})
    assert r.status_code == 200
    assert r.json()["paused"] and not r.json()["fleet_wide"]
    assert "warning" not in r.json()

    # cc's ambient writeback discarded
    r = client.post("/writeback", json={
        "session_id": "cc-jsonl-1", "summary": "CC session activity (auto-sync from JSONL)",
        "source": "tool", "category": "session_log", "agent_id": "cc",
    })
    assert r.json()["status"] == "paused"

    # opie's identical-shaped writeback LANDS
    r = client.post("/writeback", json={
        "session_id": "opie-jsonl-1", "summary": "Opie session activity (auto-sync from JSONL)",
        "source": "tool", "category": "session_log", "agent_id": "opie",
    })
    assert r.json()["status"] != "paused", r.json()
    assert r.json()["memory_id"]

    # opie's ingest lands, cc's is discarded
    assert client.post("/ingest", json={"prompt": "p", "response": "r", "agent_id": "opie"}).json()["status"] == "captured"
    assert client.post("/ingest", json={"prompt": "p", "response": "r", "agent_id": "cc"}).json()["status"] == "paused"

    # health shows the scope
    cap = client.get("/health").json()["capture"]
    assert cap["paused"] and cap["scopes"].keys() == {"cc"}

    # scoped resume
    client.post("/capture/resume", json={"agent_id": "cc"})
    assert not client.get("/capture/status").json()["paused"]


def test_endpoint_fleet_wide_pause_warns(client):
    r = client.post("/capture/pause", json={"minutes": 10, "reason": "t"})
    assert r.json()["fleet_wide"]
    assert "FLEET-WIDE" in r.json()["warning"]
    # bare resume (no body) — the old wire shape — lifts the fleet-wide pause
    assert client.post("/capture/resume").status_code == 200
    assert not client.get("/capture/status").json()["paused"]


def test_bare_resume_leaves_scoped_pauses_alone(client):
    client.post("/capture/pause", json={"minutes": 10, "agent_id": "cc"})
    client.post("/capture/pause", json={"minutes": 10})  # fleet-wide, as an old bridge would
    client.post("/capture/resume")                       # old bridge resuming
    st = client.get("/capture/status").json()
    assert st["paused"] and not st["fleet_wide"] and set(st["scopes"]) == {"cc"}, st
    client.post("/capture/resume", json={"all": True})
    assert not client.get("/capture/status").json()["paused"]


def test_pause_rejects_empty_and_star_agent_id(client):
    assert client.post("/capture/pause", json={"minutes": 1, "agent_id": ""}).status_code == 400
    assert client.post("/capture/pause", json={"minutes": 1, "agent_id": "*"}).status_code == 400
    assert client.post("/capture/resume", json={"agent_id": "*"}).status_code == 400
    assert not client.get("/capture/status").json()["paused"]


def test_shorter_repause_never_shortens_a_live_pause(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=240, reason="cc long", agent_id="cc")
    st = gate.pause(minutes=5, reason="cc3 short", agent_id="cc")   # CC3 shares tenant cc
    assert st["remaining_seconds"] > 239 * 60, "the longer pause must win"
    assert st["reason"] == "cc3 short"  # newest reason, longest window


def test_pause_reply_describes_this_pause_not_another_agents(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=240, reason="opie rotation", agent_id="opie")
    st = gate.pause(minutes=5, reason="cc quick", agent_id="cc")
    assert st["scope"] == "cc" and st["reason"] == "cc quick"
    assert st["remaining_seconds"] <= 5 * 60
    assert st["fleet_wide"] is False
