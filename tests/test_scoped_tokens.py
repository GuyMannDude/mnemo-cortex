"""v4.9 scoped tokens: per-tenant, per-endpoint auth tier.

End-to-end through create_app + TestClient: the middleware identifies the
token, the endpoint enforces the agent pin, and the master token keeps full
access. Also covers the config loader's load-time rejections.
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, CacheConfig, ClassificationConfig, ProviderConfig,
    ResilientProviderConfig, ServerConfig, ScopedToken, DEFAULT_PERSONAS,
    _parse_scoped_tokens,
)

MASTER = "master-secret"
AL_TOKEN = "al-scoped-secret"
TJ_TOKEN = "tj-scoped-secret"

_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}
VEC = [0.0] * 768
VEC[0] = 1.0


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
        cache=CacheConfig(),
        server=ServerConfig(
            port=50098,
            auth_token=MASTER,
            scoped_tokens=[
                ScopedToken(token=AL_TOKEN, agent_id="al",
                            endpoints=["/context", "/writeback"]),
                ScopedToken(token=TJ_TOKEN, agent_id="tj",
                            endpoints=["/trajectory/save", "/trajectory/recall"]),
            ],
        ),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


def _writeback_body(agent_id):
    return {"agent_id": agent_id, "summary": "scoped write test",
            "key_facts": ["fact one"], "session_id": "scoped-test-1"}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_health_needs_no_token(client):
    assert client.get("/health").status_code == 200


def test_master_token_full_access(client):
    r = client.post("/writeback", json=_writeback_body("cc"), headers=_auth(MASTER))
    assert r.status_code == 200
    r = client.get("/sessions", headers=_auth(MASTER))
    assert r.status_code == 200


def test_no_token_rejected(client):
    assert client.post("/writeback", json=_writeback_body("al")).status_code == 401


def test_unknown_token_rejected(client):
    r = client.post("/writeback", json=_writeback_body("al"), headers=_auth("wrong"))
    assert r.status_code == 401


def test_scoped_token_allowed_endpoint_and_agent(client):
    r = client.post("/writeback", json=_writeback_body("al"), headers=_auth(AL_TOKEN))
    assert r.status_code == 200
    r = client.post("/context", json={"agent_id": "al", "prompt": "scoped write"},
                    headers=_auth(AL_TOKEN))
    assert r.status_code == 200


def test_scoped_token_wrong_agent_403(client):
    r = client.post("/writeback", json=_writeback_body("cc"), headers=_auth(AL_TOKEN))
    assert r.status_code == 403
    assert "scoped to agent 'al'" in r.text


def test_scoped_token_missing_agent_403(client):
    # No agent_id would land in the "default" tenant — must be refused.
    body = _writeback_body("al")
    del body["agent_id"]
    r = client.post("/writeback", json=body, headers=_auth(AL_TOKEN))
    assert r.status_code == 403


def test_scoped_token_out_of_scope_endpoint_403(client):
    r = client.post("/trajectory/recall", json={"agent_id": "al", "query": "q"},
                    headers=_auth(AL_TOKEN))
    assert r.status_code == 403
    r = client.get("/sessions", headers=_auth(AL_TOKEN))
    assert r.status_code == 403


def test_scoped_write_lands_in_pinned_tenant_only(client, tmp_path):
    r = client.post("/writeback", json=_writeback_body("al"), headers=_auth(AL_TOKEN))
    assert r.status_code == 200
    agents_root = tmp_path / "agents"
    assert (agents_root / "al").is_dir()
    assert not (agents_root / "cc").exists()


def test_scoped_token_x_api_key_header(client):
    r = client.post("/writeback", json=_writeback_body("al"),
                    headers={"X-API-KEY": AL_TOKEN})
    assert r.status_code == 200
    r = client.post("/writeback", json=_writeback_body("cc"),
                    headers={"X-API-KEY": AL_TOKEN})
    assert r.status_code == 403


def test_scoped_path_tricks_fail_closed(client):
    # The allowlist matches request.url.path EXACTLY, before routing — so
    # trailing-slash, case, and dot-segment variants must 403, never redirect
    # onto a real route past the allowlist.
    for path in ("/writeback/", "/Writeback", "/context/../sessions",
                 "/context/", "/writeback%20"):
        r = client.post(path, json=_writeback_body("al"), headers=_auth(AL_TOKEN))
        assert r.status_code == 403, f"{path} returned {r.status_code}"
    # Query strings ride along fine — .path excludes them.
    r = client.post("/writeback?x=1", json=_writeback_body("al"),
                    headers=_auth(AL_TOKEN))
    assert r.status_code == 200


def _traj_save_body(agent_id):
    return {"agent_id": agent_id, "task_type": "test", "task_description": "t",
            "steps": [{"action": "did the thing"}], "outcome": "worked",
            "rating": 4}


def test_trajectory_endpoints_enforce_pin(client):
    r = client.post("/trajectory/save", json=_traj_save_body("tj"),
                    headers=_auth(TJ_TOKEN))
    assert r.status_code == 200
    r = client.post("/trajectory/save", json=_traj_save_body("cc"),
                    headers=_auth(TJ_TOKEN))
    assert r.status_code == 403
    r = client.post("/trajectory/recall", json={"agent_id": "tj", "query": "thing"},
                    headers=_auth(TJ_TOKEN))
    assert r.status_code == 200
    r = client.post("/trajectory/recall", json={"agent_id": "al", "query": "thing"},
                    headers=_auth(TJ_TOKEN))
    assert r.status_code == 403


# ── config loader validation ──

def test_parse_rejects_empty_token():
    with pytest.raises(ValueError, match="token is empty"):
        _parse_scoped_tokens([{"token": "${UNSET_ENV_VAR_XYZ}", "agent_id": "al",
                               "endpoints": ["/context"]}])


def test_parse_rejects_missing_agent():
    with pytest.raises(ValueError, match="agent_id is required"):
        _parse_scoped_tokens([{"token": "t", "endpoints": ["/context"]}])


def test_parse_rejects_unscopable_endpoint():
    with pytest.raises(ValueError, match="cannot be scoped"):
        _parse_scoped_tokens([{"token": "t", "agent_id": "al",
                               "endpoints": ["/context", "/facts"]}])


def test_parse_rejects_empty_endpoints():
    with pytest.raises(ValueError, match="non-empty list"):
        _parse_scoped_tokens([{"token": "t", "agent_id": "al", "endpoints": []}])


def test_parse_accepts_valid_entry():
    toks = _parse_scoped_tokens([{"token": "t", "agent_id": "al",
                                  "endpoints": ["/context", "/trajectory/recall"]}])
    assert len(toks) == 1
    assert toks[0].agent_id == "al"
    assert set(toks[0].endpoints) == {"/context", "/trajectory/recall"}
