"""The five /passport/* HTTP routes (clean-room review H10).

Until v4.9.11 only the validation layer had tests; the HTTP surface — the
contract every MCP bridge actually calls — had none. These tests exercise each
route happy-path and failure-path through a real TestClient app, with the
passport store isolated to tmp_path.
"""
from __future__ import annotations

from pathlib import Path

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
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "x"
    async def health_check(self): return True


@pytest.fixture
def passport_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MNEMO_PASSPORT_DIR", str(tmp_path / "passport-data"))
    from passport import config as config_mod
    config_mod.reload()

    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50099),
        data_dir=str(tmp_path / "agentb-data"),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as client:
            yield client
    config_mod.reload()


def _observe_payload(**overrides) -> dict:
    payload = {
        "proposed_claim": "Prefers numbered steps for long procedures",
        "type": "preference",
        "source_platform": "cc",
        "source_session_id": "sess_api_001",
        "evidence": [
            {"turn_ref": "turn-1", "excerpt": "user said: give me numbered steps"},
            {"turn_ref": "turn-2", "excerpt": "user reordered my prose into a list"},
        ],
    }
    payload.update(overrides)
    return payload


# ─── /context ────────────────────────────────────────────────────────────────

def test_context_empty_store(passport_client):
    r = passport_client.post("/passport/context", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["claims"] == []
    assert body["passport_version"]
    assert isinstance(body["prompt_block"], str)


def test_context_rejects_bad_max_claims(passport_client):
    r = passport_client.post("/passport/context", json={"max_claims": 0})
    assert r.status_code == 422


# ─── /observe ────────────────────────────────────────────────────────────────

def test_observe_clean_claim_goes_pending(passport_client):
    r = passport_client.post("/passport/observe", json=_observe_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["observation_id"].startswith("obs_")
    assert body["disposition"] == "allow"
    # The observe was git-committed in the passport data dir.
    assert body["commit_sha"]


def test_observe_secret_rejected_never_enters_pending(passport_client):
    r = passport_client.post("/passport/observe", json=_observe_payload(
        proposed_claim="Paste sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 before run",
    ))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["disposition"] == "hard_block"
    assert body["observation_id"] is None
    # Hard-blocked content must not be queued.
    r2 = passport_client.post("/passport/pending", json={})
    assert r2.json()["items"] == []


def test_observe_requires_two_evidence(passport_client):
    r = passport_client.post("/passport/observe", json=_observe_payload(
        evidence=[{"turn_ref": "turn-1", "excerpt": "only one excerpt"}],
    ))
    assert r.status_code == 422


def test_observe_rejects_oversize_claim(passport_client):
    r = passport_client.post("/passport/observe", json=_observe_payload(
        proposed_claim="x" * 181,
    ))
    assert r.status_code == 422


def test_observe_rejects_oversize_evidence_list(passport_client):
    # M-group (clean-room review): evidence drives O(rows × detectors) regex
    # work on a network endpoint — the list needs a ceiling.
    rows = [{"turn_ref": f"turn-{i}", "excerpt": f"evidence row {i}"}
            for i in range(65)]
    r = passport_client.post("/passport/observe", json=_observe_payload(
        evidence=rows,
    ))
    assert r.status_code == 422


# ─── /pending ────────────────────────────────────────────────────────────────

def test_pending_lists_observed_item(passport_client):
    obs_id = passport_client.post(
        "/passport/observe", json=_observe_payload()
    ).json()["observation_id"]

    r = passport_client.post("/passport/pending", json={})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i for i in items if i["observation_id"] == obs_id]


# ─── /promote ────────────────────────────────────────────────────────────────

def test_promote_full_flow_reaches_context(passport_client):
    obs_id = passport_client.post(
        "/passport/observe", json=_observe_payload()
    ).json()["observation_id"]

    r = passport_client.post("/passport/promote", json={"observation_id": obs_id})
    assert r.status_code == 200
    body = r.json()
    assert body["promoted"] is True
    assert body["claim_id"]
    assert body["commit_sha"]

    ctx = passport_client.post("/passport/context", json={}).json()
    assert any(c["claim_id"] == body["claim_id"] for c in ctx["claims"])


def test_promote_unknown_id_fails_with_reason(passport_client):
    r = passport_client.post("/passport/promote", json={"observation_id": "obs_nope"})
    assert r.status_code == 200
    body = r.json()
    assert body["promoted"] is False
    assert body["reason"]


# ─── /override ───────────────────────────────────────────────────────────────

def test_override_deprecates_promoted_claim(passport_client):
    obs_id = passport_client.post(
        "/passport/observe", json=_observe_payload()
    ).json()["observation_id"]
    claim_id = passport_client.post(
        "/passport/promote", json={"observation_id": obs_id}
    ).json()["claim_id"]

    r = passport_client.post("/passport/override", json={
        "action": "deprecate", "target_claim_id": claim_id, "reason": "test",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["action"] == "deprecate"

    ctx = passport_client.post("/passport/context", json={}).json()
    assert not any(c["claim_id"] == claim_id for c in ctx["claims"])


def test_override_action_override_requires_replacement(passport_client):
    r = passport_client.post("/passport/override", json={
        "action": "override", "target_claim_id": "clm_whatever",
    })
    assert r.status_code == 422


def test_override_unknown_claim_fails_with_reason(passport_client):
    r = passport_client.post("/passport/override", json={
        "action": "forget", "target_claim_id": "clm_nope",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["reason"]
