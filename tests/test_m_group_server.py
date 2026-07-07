"""M-group server hardening regression tests (clean-room review, S126).

M1: the body-size cap is enforced while the body streams — omitting
    Content-Length (chunked transfer encoding) used to skip the check.
M2: a maintenance cycle iterates a snapshot of the tenant dict — a tenant
    created by a live request mid-cycle used to raise "dict changed size
    during iteration" and silently kill the whole background loop.
M4: /preflight returns UNAVAILABLE when validation itself failed — it used
    to return PASS exactly when it couldn't validate (fail-open).
M5: /preflight redacts prompt/draft before they reach the reasoner, and
    enforces the scoped-token tenant pin like every other tenant endpoint.

(M6 — the passport evidence-list cap — is tested in tests/passport/test_api.py.)
"""
import asyncio

from unittest.mock import patch
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, CacheConfig, ClassificationConfig, ProviderConfig,
    ResilientProviderConfig, ScopedToken, ServerConfig, DEFAULT_PERSONAS,
    SCOPABLE_ENDPOINTS,
)

MASTER = "master-secret"
SCOPED = "scoped-secret"
SECRET_VALUE = "abcdef1234567890XYZ"  # matches redact.py generic-assignment

_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}
VEC = [0.0] * 768
VEC[0] = 1.0


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)
    async def health_check(self): return True


class TenantCreatingEmbedding(FakeEmbedding):
    """Simulates a live request creating a tenant while a maintenance cycle
    is mid-iteration: the first embed after arm() inserts a new tenant."""
    def __init__(self):
        self.tenant_mgr = None
        self.armed = False

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        if self.armed and self.tenant_mgr is not None:
            self.armed = False
            self.tenant_mgr.get("newcomer")
        return list(VEC)


class VerdictReasoning:
    """Returns a canned reply and records every prompt it was shown."""
    active_label = "fake/reason"
    def __init__(self, reply='{"verdict": "pass", "confidence": 0.9, "reason": "ok"}'):
        self.reply = reply
        self.prompts: list[str] = []

    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        self.prompts.append(prompt)
        return self.reply
    async def health_check(self): return True


class DownReasoning(VerdictReasoning):
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        raise RuntimeError("reasoner down")


def make_app(tmp_path, reasoner=None, embedder=None, **server_kw):
    server_kw.setdefault("port", 50098)
    server_kw.setdefault("auth_token", MASTER)
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(),
        server=ServerConfig(**server_kw),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding",
               return_value=embedder or FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning",
               return_value=reasoner or VerdictReasoning()):
        from agentb.server import create_app
        return create_app(cfg)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _writeback_body(agent_id="cc", summary="a memory to precache"):
    return {"agent_id": agent_id, "session_id": "s1", "summary": summary,
            "key_facts": ["x"]}


# ── M1: body-size cap enforced while streaming ──

def test_content_length_over_cap_rejected(tmp_path):
    app = make_app(tmp_path, max_body_bytes=1024)
    with TestClient(app) as c:
        r = c.post("/writeback", content=b"x" * 2048,
                   headers={**_auth(MASTER), "Content-Type": "application/json"})
        assert r.status_code == 413


def test_chunked_body_over_cap_rejected(tmp_path):
    app = make_app(tmp_path, max_body_bytes=1024)

    def chunks():
        for _ in range(8):
            yield b"x" * 512  # 4 KB total, no Content-Length header

    with TestClient(app) as c:
        r = c.post("/writeback", content=chunks(),
                   headers={**_auth(MASTER), "Content-Type": "application/json"})
        assert r.status_code == 413


def test_body_under_cap_still_works(tmp_path):
    app = make_app(tmp_path, max_body_bytes=1024 * 1024)
    with TestClient(app) as c:
        r = c.post("/writeback", json=_writeback_body(), headers=_auth(MASTER))
        assert r.status_code == 200


# ── M2: maintenance cycle vs. concurrent tenant creation ──

def test_maintenance_cycle_survives_tenant_created_mid_cycle(tmp_path):
    embedder = TenantCreatingEmbedding()
    app = make_app(tmp_path, embedder=embedder)
    with TestClient(app) as c:
        # Two tenants with a memory each so precache has embeds to run.
        assert c.post("/writeback", json=_writeback_body("cc"),
                      headers=_auth(MASTER)).status_code == 200
        assert c.post("/writeback", json=_writeback_body("rocky"),
                      headers=_auth(MASTER)).status_code == 200

        embedder.tenant_mgr = app.state.tenants
        embedder.armed = True
        # Pre-fix this raised RuntimeError("dictionary changed size during
        # iteration") out of the cycle and killed the maintenance loop.
        asyncio.run(app.state.maintenance_cycle(1))

        assert "newcomer" in app.state.tenants._tenants
        # And the next cycle picks the newcomer up without incident.
        asyncio.run(app.state.maintenance_cycle(2))


# ── M4: preflight fails UNAVAILABLE, not PASS ──

def test_preflight_happy_path_still_passes(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/preflight", json={"prompt": "hi", "draft_response": "hello",
                                       "agent_id": "cc"}, headers=_auth(MASTER))
        assert r.status_code == 200
        assert r.json()["verdict"] == "PASS"


def test_preflight_reasoner_outage_is_unavailable(tmp_path):
    app = make_app(tmp_path, reasoner=DownReasoning())
    with TestClient(app) as c:
        r = c.post("/preflight", json={"prompt": "hi", "draft_response": "hello",
                                       "agent_id": "cc"}, headers=_auth(MASTER))
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "UNAVAILABLE"
        assert body["confidence"] == 0.0


def test_preflight_garbage_json_is_unavailable(tmp_path):
    app = make_app(tmp_path, reasoner=VerdictReasoning(reply="not json at all"))
    with TestClient(app) as c:
        r = c.post("/preflight", json={"prompt": "hi", "draft_response": "hello",
                                       "agent_id": "cc"}, headers=_auth(MASTER))
        assert r.json()["verdict"] == "UNAVAILABLE"


def test_preflight_missing_verdict_key_is_unavailable(tmp_path):
    app = make_app(tmp_path, reasoner=VerdictReasoning(reply='{"confidence": 0.9}'))
    with TestClient(app) as c:
        r = c.post("/preflight", json={"prompt": "hi", "draft_response": "hello",
                                       "agent_id": "cc"}, headers=_auth(MASTER))
        assert r.json()["verdict"] == "UNAVAILABLE"


# ── M5: preflight redaction + scoped-token pin ──

def test_preflight_redacts_before_reasoner(tmp_path):
    reasoner = VerdictReasoning()
    app = make_app(tmp_path, reasoner=reasoner)
    with TestClient(app) as c:
        r = c.post("/preflight", json={
            "prompt": f'set api_key = "{SECRET_VALUE}" in the config',
            "draft_response": f"Authorization: Bearer {SECRET_VALUE}{SECRET_VALUE}",
            "agent_id": "cc",
        }, headers=_auth(MASTER))
        assert r.status_code == 200
    assert reasoner.prompts, "reasoner never saw the preflight prompt"
    sent = reasoner.prompts[-1]
    assert SECRET_VALUE not in sent
    assert "[REDACTED:" in sent


def test_preflight_is_scopable():
    assert "/preflight" in SCOPABLE_ENDPOINTS


def test_preflight_enforces_scoped_token_pin(tmp_path):
    app = make_app(
        tmp_path,
        scoped_tokens=[ScopedToken(token=SCOPED, agent_id="cc",
                                   endpoints=["/preflight"])],
    )
    with TestClient(app) as c:
        cross = c.post("/preflight", json={"prompt": "hi", "draft_response": "x",
                                           "agent_id": "rocky"}, headers=_auth(SCOPED))
        assert cross.status_code == 403

        own = c.post("/preflight", json={"prompt": "hi", "draft_response": "x",
                                         "agent_id": "cc"}, headers=_auth(SCOPED))
        assert own.status_code == 200
