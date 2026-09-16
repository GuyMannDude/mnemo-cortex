"""v4.23 authority tiers — the server half.

The read rule that would have saved 2026-09-15: a locked fact about an
entity the prompt names is served ahead of every soft memory, and the
newer-but-wrong inferred sentence no longer leads. Plus the routes that
lock a slot, list/resolve proposals, and demote a memory by id.
"""
from __future__ import annotations

import json

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
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c, tmp_path


def _writeback(client, summary, source="inferred", session="s-auth"):
    r = client.post("/writeback", json={
        "session_id": session, "summary": summary, "key_facts": [],
        "category": "topology", "source": source, "force": True,
    })
    assert r.status_code == 200, r.text
    return r.json()["memory_id"]


def _lock_igor2(client):
    r = client.post("/facts", json={
        "entity": "igor-2", "attribute": "access_paths_from_igor",
        "value": "SSH (22) + RDP (3389) over Tailscale 198.51.100.5 AND LAN 192.0.2.122",
        "confidence": "verified", "evidence_source": "statement:guy 2026-09-15 + tool:nc",
        "source_agent": "cc",
    })
    assert r.status_code == 200 and r.json()["written"], r.text
    assert r.json()["authority"] == "open" and r.json()["proposal_id"] is None
    r = client.post("/facts/authority", json={
        "entity": "igor-2", "attribute": "access_paths_from_igor", "authority": "probe",
        "evidence_source": "statement:guy S337", "changed_by": "cc",
        "probe_cmd": "nc -z 198.51.100.5 22", "probe_host": "igor",
    })
    assert r.status_code == 200 and r.json()["written"], r.text
    assert r.json()["authority"] == "probe"


def test_locked_fact_leads_the_window_over_a_newer_wrong_memory(client):
    c, _ = client
    _writeback(c, "IGOR-2 is reachable by SSH over Tailscale and the LAN", source="user")
    bad = _writeback(c, "Tailscale serves as the exclusive connectivity path for agents accessing the IGOR-2 host")
    _lock_igor2(c)

    r = c.post("/context", json={"prompt": "how do I reach igor-2 from the laptop", "max_results": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    chunks = body["chunks"]
    assert chunks[0]["cache_tier"] == "FACT"
    assert chunks[0]["content"].startswith("[FACT:probe] igor-2.access_paths_from_igor = SSH (22) + RDP (3389)")
    assert chunks[0]["provenance_source"] == "tool"
    assert "authority:probe" in chunks[0]["additional_tags"]
    assert chunks[0]["memory_id"] is None
    # the soft memories still serve, after the fact, inside the same window size
    assert len(chunks) <= 4
    assert any(ch["memory_id"] == bad for ch in chunks[1:])
    assert body["cache_hits"]["FACT"] == 1
    assert body["total_found"] == len(chunks)


def test_no_locked_fact_no_pin(client):
    c, _ = client
    _writeback(c, "IGOR-2 notes")
    r = c.post("/context", json={"prompt": "igor-2", "max_results": 3})
    assert r.status_code == 200
    assert all(ch["cache_tier"] != "FACT" for ch in r.json()["chunks"])
    assert "FACT" not in r.json()["cache_hits"]


def test_pin_takes_at_most_half_the_window(client):
    c, _ = client
    for i in range(3):
        _writeback(c, f"igor-2 memory {i}", session=f"s{i}")
    _lock_igor2(c)
    for attr in ("motherboard", "bios_version"):
        c.post("/facts", json={"entity": "igor-2", "attribute": attr, "value": "x",
                               "confidence": "verified", "evidence_source": "tool:dmidecode"})
        c.post("/facts/authority", json={"entity": "igor-2", "attribute": attr, "authority": "probe",
                                         "evidence_source": "statement:guy", "probe_cmd": "dmidecode"})
    r = c.post("/context", json={"prompt": "igor-2", "max_results": 4})
    chunks = r.json()["chunks"]
    assert [ch["cache_tier"] for ch in chunks[:2]] == ["FACT", "FACT"]
    assert len(chunks) == 4 and chunks[2]["cache_tier"] != "FACT"
    r = c.post("/context", json={"prompt": "igor-2", "max_results": 1})
    chunks = r.json()["chunks"]
    assert len(chunks) == 1 and chunks[0]["cache_tier"] == "FACT"


def test_proposal_routes(client):
    c, _ = client
    _lock_igor2(c)
    r = c.post("/facts", json={"entity": "igor-2", "attribute": "access_paths_from_igor",
                               "value": "Tailscale only", "confidence": "high_probability",
                               "evidence_source": "dream:2026-09-16", "source_agent": "dreamer"})
    assert r.status_code == 200
    assert r.json()["written"] is False and r.json()["reason"].startswith("locked:probe")
    pid = r.json()["proposal_id"]
    assert pid == 1

    r = c.get("/facts/proposals")
    assert r.json()["count"] == 1 and r.json()["proposals"][0]["proposed_value"] == "Tailscale only"
    assert c.get("/facts/proposals", params={"status": "bogus"}).status_code == 400

    r = c.post(f"/facts/proposals/{pid}/resolve", json={"action": "reject", "by": "cc", "reason": "wrong"})
    assert r.status_code == 200 and r.json()["written"] is False
    assert c.get("/facts/proposals").json()["count"] == 0
    assert c.get("/facts/proposals", params={"status": "all"}).json()["count"] == 1
    assert c.post(f"/facts/proposals/{pid}/resolve", json={"action": "eat", "by": "cc"}).status_code == 400
    assert c.get("/facts/igor-2/access_paths_from_igor").json()["value"].startswith("SSH (22)")

    # authority route validation
    assert c.post("/facts/authority", json={"entity": "igor-2", "attribute": "access_paths_from_igor",
                                            "authority": "hard", "evidence_source": "statement:guy"}).status_code == 400
    r = c.post("/facts/authority", json={"entity": "igor-2", "attribute": "access_paths_from_igor",
                                         "authority": "declared", "evidence_source": "tool:x"})
    assert r.status_code == 200 and r.json()["written"] is False


def test_memory_demote_route(client):
    c, tmp_path = client
    mid = _writeback(c, "Tailscale is the only path to IGOR-2")
    r = c.post("/context", json={"prompt": "path to igor-2", "max_results": 3})
    assert mid in [ch["memory_id"] for ch in r.json()["chunks"]]

    assert c.post("/memories/demote", json={"memory_id": mid, "reason": "  "}).status_code == 400
    assert c.post("/memories/demote", json={"memory_id": "nope", "reason": "x"}).status_code == 404

    r = c.post("/memories/demote", json={"memory_id": mid, "reason": "agent's loose sentence", "by": "cc"})
    assert r.status_code == 200, r.text
    assert r.json()["demoted"] is True and r.json()["superseded_by"].startswith("demoted:cc:")

    rec = json.loads(next(tmp_path.rglob(f"{mid}.json")).read_text())
    assert rec["superseded_by"].startswith("demoted:cc:") and rec["demote_reason"] == "agent's loose sentence"
    assert rec["summary"] == "Tailscale is the only path to IGOR-2"   # JSON kept

    r = c.post("/context", json={"prompt": "path to igor-2", "max_results": 3})
    assert mid not in [ch["memory_id"] for ch in r.json()["chunks"]]

    r = c.post("/memories/demote", json={"memory_id": mid, "reason": "again", "by": "cc"})
    assert r.status_code == 200 and r.json()["demoted"] is False
    assert "already superseded" in r.json()["reason"]


# ── review 2026-09-15 on 7fda6c4 ──────────────────────────────────────────

def test_memory_ids_that_are_paths_are_refused(client):
    """#5: `memory_dir / f"{id}.json"` must never leave the tenant directory."""
    c, tmp_path = client
    mid = _writeback(c, "a real memory about igor-2")
    for bad in ("../x", "..", "a/b", "a\\b", ".hidden", "", "x" * 129, "id.json"):
        r = c.post("/memories/demote", json={"memory_id": bad, "reason": "x", "by": "cc"})
        assert r.status_code == 400, (bad, r.status_code, r.text)
        r = c.post("/writeback", json={"session_id": "s-bad", "summary": "new truth", "key_facts": [],
                                       "category": "topology", "force": True, "supersedes": [mid, bad]})
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert "supersedes" in r.text
    # the real id still works on both routes
    new = _writeback(c, "the corrected memory", session="s-good")
    r = c.post("/writeback", json={"session_id": "s-sup", "summary": "supersedes the first", "key_facts": [],
                                   "category": "topology", "force": True, "supersedes": [mid]})
    assert r.status_code == 200, r.text
    assert c.post("/memories/demote", json={"memory_id": new, "reason": "x", "by": "cc"}).status_code == 200


def test_fact_demote_route_reports_a_hold(client):
    """#1 over HTTP: the route carries evidence and names the proposal."""
    c, _ = client
    _lock_igor2(c)
    r = c.post("/facts/demote", json={"entity": "igor-2", "attribute": "access_paths_from_igor",
                                      "reason": "loose sentence", "changed_by": "rocky"})
    assert r.status_code == 200
    assert r.json()["written"] is False and r.json()["proposal_id"] == 1 and r.json()["authority"] == "probe"
    assert c.get("/facts/igor-2/access_paths_from_igor").json()["confidence"] == "verified"
    assert c.get("/facts/proposals").json()["proposals"][0]["confidence"] == "false"
    r = c.post("/facts/demote", json={"entity": "igor-2", "attribute": "access_paths_from_igor",
                                      "reason": "port closed", "evidence_source": "tool:nc -z -> closed"})
    assert r.json()["written"] is True and r.json()["proposal_id"] is None


@pytest.fixture
def scoped_client(tmp_path):
    from agentb.config import ScopedToken
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(),
        server=ServerConfig(host="127.0.0.1", port=50098, auth_token="master-token",
                            scoped_tokens=[ScopedToken(token="partner-token", agent_id="cc",
                                                       endpoints=["/context", "/writeback"])]),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


def test_scoped_token_gets_no_locked_fact_pin(scoped_client):
    """#6: the facts store is global, the tenant is not — a partner token must
    never read Guy's declared facts. Under-share is the only allowed failure."""
    c = scoped_client
    master = {"X-API-KEY": "master-token"}
    partner = {"X-API-KEY": "partner-token"}
    r = c.post("/writeback", json={"session_id": "s1", "summary": "igor-2 has a nice case", "key_facts": [],
                                   "category": "topology", "source": "inferred", "force": True, "agent_id": "cc"},
               headers=master)
    assert r.status_code == 200, r.text
    r = c.post("/facts", json={"entity": "igor-2", "attribute": "access_paths_from_igor", "value": "SSH+RDP both IPs",
                               "confidence": "verified", "evidence_source": "statement:guy"}, headers=master)
    assert r.status_code == 200 and r.json()["written"]
    r = c.post("/facts/authority", json={"entity": "igor-2", "attribute": "access_paths_from_igor",
                                         "authority": "declared", "evidence_source": "statement:guy"}, headers=master)
    assert r.status_code == 200 and r.json()["written"]
    # /facts is not scopable at all
    assert c.post("/facts", json={"entity": "x", "attribute": "y", "value": "z", "confidence": "verified",
                                  "evidence_source": "statement:guy"}, headers=partner).status_code == 403

    body = {"prompt": "how do I reach igor-2", "max_results": 4, "agent_id": "cc"}
    tiers_master = [ch["cache_tier"] for ch in c.post("/context", json=body, headers=master).json()["chunks"]]
    tiers_partner = [ch["cache_tier"] for ch in c.post("/context", json=body, headers=partner).json()["chunks"]]
    assert tiers_master[0] == "FACT"
    assert "FACT" not in tiers_partner and tiers_partner   # memories still served, no fact
