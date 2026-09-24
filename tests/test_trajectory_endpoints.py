"""End-to-end HTTP tests for the v4.5 trajectory endpoints through create_app.

Uses the same FakeEmbedding harness as test_context_vec_filter: every input
embeds to one fixed vector, so similarity is uniform and the rating/recency
ranking + the min_rating / task_type filters are what's under test here.
Similarity *ordering* is proven separately in test_trajectory.py with distinct
per-trajectory vectors.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

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


def _save(client, *, task_type="bus_debug", rating=5, desc="fix the bus path", agent_id="cc"):
    r = client.post("/trajectory/save", json={
        "agent_id": agent_id,
        "task_type": task_type,
        "task_description": desc,
        "steps": [{"action": "grep config", "tool_used": "bash", "result_summary": "found stale path"}],
        "outcome": "bus reads correct db",
        "rating": rating,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_save_then_recall_roundtrip(client):
    saved = _save(client, desc="repoint rocky bus at the right database")
    assert saved["status"] == "saved"
    assert saved["trajectory_id"]
    assert saved["total_for_agent"] == 1

    r = client.post("/trajectory/recall", json={
        "agent_id": "cc",
        "query": "agent bus read path pointing at wrong database",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_found"] == 1
    t = body["trajectories"][0]
    assert t["id"] == saved["trajectory_id"]
    assert t["steps"][0]["action"] == "grep config"   # full recipe returned
    assert t["rating"] == 5
    assert "_score" in t


def test_recall_min_rating_filters(client):
    _save(client, rating=2, desc="low quality run")
    r = client.post("/trajectory/recall", json={
        "agent_id": "cc", "query": "anything", "min_rating": 4,
    })
    assert r.status_code == 200
    assert r.json()["total_found"] == 0


def test_recall_task_type_filter(client):
    _save(client, task_type="bus_debug", desc="bus one")
    _save(client, task_type="shopify_fix", desc="shopify one")
    r = client.post("/trajectory/recall", json={
        "agent_id": "cc", "query": "anything", "task_type": "shopify_fix", "max_results": 5,
    })
    assert r.status_code == 200
    trajs = r.json()["trajectories"]
    assert len(trajs) == 1
    assert trajs[0]["task_type"] == "shopify_fix"


def test_tenant_isolation(client):
    """An agent only recalls its OWN trajectories (per-agent data dir)."""
    _save(client, agent_id="cc", desc="cc private recipe")
    r = client.post("/trajectory/recall", json={
        "agent_id": "opie", "query": "cc private recipe",
    })
    assert r.status_code == 200
    assert r.json()["total_found"] == 0


def test_save_validation_rejects_bad_rating(client):
    r = client.post("/trajectory/save", json={
        "agent_id": "cc", "task_type": "x", "task_description": "d",
        "steps": [{"action": "a"}], "outcome": "o", "rating": 9,  # out of 1–5
    })
    assert r.status_code == 422  # pydantic validation


def test_recall_empty_is_clean(client):
    r = client.post("/trajectory/recall", json={"agent_id": "cc", "query": "nothing here"})
    assert r.status_code == 200
    assert r.json() == {"trajectories": [], "total_found": 0, "agent_id": "cc"}


def test_inspection_4_trajectory_save_redacts(client, tmp_path):
    """/trajectory/save was the one write door with no redaction: secrets in
    the description, step args, outcome or evidence_source were embedded and
    stored verbatim (code-inspection-2026-09-24-cc3.md #4)."""
    key = "sk-ant-api03-" + "Q" * 40
    pw = "Zx9qR4tLm2Vb7Kp1Wd"
    r = client.post("/trajectory/save", json={
        "agent_id": "cc", "task_type": "deploy",
        "task_description": f"rotate {key}",
        "steps": [{"action": "curl", "tool_used": "bash",
                   "args": {"headers": {"Authorization": f"Bearer {key}"},
                            "password": pw}}],
        "outcome": f"worked with {key}", "rating": 5,
        "evidence_source": f"tool:{key}",
    })
    assert r.status_code == 200, r.text
    on_disk = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert key.encode() not in on_disk and pw.encode() not in on_disk
    assert b"[REDACTED:" in on_disk
    got = client.post("/trajectory/recall", json={"agent_id": "cc", "query": "rotate"}).json()
    assert key not in str(got) and pw not in str(got)
    assert got["trajectories"][0]["steps"][0]["action"] == "curl"   # recipe intact
