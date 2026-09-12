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
import json
import time

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


def _writeback_body(agent_id="cc", summary="a memory to recall"):
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
    app = make_app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/writeback", json=_writeback_body("cc"),
                      headers=_auth(MASTER)).status_code == 200
        assert c.post("/writeback", json=_writeback_body("rocky"),
                      headers=_auth(MASTER)).status_code == 200

        # A live request creates a tenant while the cycle is inside another
        # tenant's step. (The L1 precache step that used to host this
        # trigger went with the tier; session archival runs for every tenant.)
        tenants = app.state.tenants
        real_archive = tenants._tenants["cc"]["sessions"].archive_hot_sessions

        async def creating_archive(summarize):
            tenants.get("newcomer")
            return await real_archive(summarize)

        tenants._tenants["cc"]["sessions"].archive_hot_sessions = creating_archive
        # Pre-fix this raised RuntimeError("dictionary changed size during
        # iteration") out of the cycle and killed the maintenance loop.
        asyncio.run(app.state.maintenance_cycle(1))

        assert "newcomer" in tenants._tenants
        # And the next cycle picks the newcomer up without incident.
        asyncio.run(app.state.maintenance_cycle(2))


# ── E3 follow-up: preflight's memory block comes from VEC ──

def test_preflight_memory_context_comes_from_vec(tmp_path):
    reasoner = VerdictReasoning()
    app = make_app(tmp_path, reasoner=reasoner)
    with TestClient(app) as c:
        assert c.post("/writeback", json=_writeback_body("cc", summary="the anvil laptop is the launchpad"),
                      headers=_auth(MASTER)).status_code == 200
        r = c.post("/preflight", json={"prompt": "which machine is the launchpad",
                                       "draft_response": "the anvil", "agent_id": "cc"},
                   headers=_auth(MASTER))
        assert r.status_code == 200, r.text
    assert reasoner.prompts, "reasoner never saw a prompt"
    assert "MEMORY CONTEXT:" in reasoner.prompts[-1]
    assert "[VEC] " in reasoner.prompts[-1] and "the anvil laptop is the launchpad" in reasoner.prompts[-1]


def test_preflight_memory_block_absent_on_empty_index_and_hides_session_log(tmp_path):
    """Two negatives that make the positive above mean something: no
    memories → no block at all (not an empty block); a session_log memory
    (raw auto-capture, archived-session summaries) is hidden from the
    verdict exactly as /context hides it by default (review, E3 follow-up)."""
    reasoner = VerdictReasoning()
    app = make_app(tmp_path, reasoner=reasoner)
    with TestClient(app) as c:
        r = c.post("/preflight", json={"prompt": "anything", "draft_response": "x", "agent_id": "cc"},
                   headers=_auth(MASTER))
        assert r.status_code == 200, r.text
        assert "MEMORY CONTEXT:" not in reasoner.prompts[-1]

        body = _writeback_body("cc", summary="[AUTO-CAPTURE] 3 tool calls: ran the suite")
        body["category"] = "session_log"
        assert c.post("/writeback", json=body, headers=_auth(MASTER)).status_code == 200
        r = c.post("/preflight", json={"prompt": "anything", "draft_response": "x", "agent_id": "cc"},
                   headers=_auth(MASTER))
        assert r.status_code == 200, r.text
        assert "AUTO-CAPTURE" not in reasoner.prompts[-1]
        assert "MEMORY CONTEXT:" not in reasoner.prompts[-1]


# ── tenant isolation at the recall surface ──

def test_context_never_serves_another_tenants_memory(tmp_path):
    """The deleted L2 isolation test built two indexes by hand and never went
    through the server. This is the property that matters: agent A's recall
    excludes agent B's memory even when the vectors are identical."""
    app = make_app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/writeback", json=_writeback_body("cc", summary="cc's private note about the anvil"),
                      headers=_auth(MASTER)).status_code == 200
        assert c.post("/writeback", json=_writeback_body("rocky", summary="rocky's private note about the bellows"),
                      headers=_auth(MASTER)).status_code == 200
        r = c.post("/context", json={"prompt": "private note", "max_results": 5, "agent_id": "cc"},
                   headers=_auth(MASTER))
        assert r.status_code == 200, r.text
        texts = " ".join(ch["content"] for ch in r.json()["chunks"])
        assert "anvil" in texts
        assert "bellows" not in texts


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


# ── v4.21.5: an archived session's memory is dated by the session, not the janitor ──

def test_archived_session_memory_carries_exchange_time_not_archival_time(tmp_path):
    """The 2026-09-12 brief specimen: a dormant tenant's months-old hot
    sessions get archived on its first load, and the session_log memory
    used to carry the ARCHIVAL time — so the dreamer read April events as
    last night's. The memory's timestamp must be the session's own
    last_exchange; created_at still marks the write for recall decay."""
    app = make_app(tmp_path, reasoner=VerdictReasoning(reply="cc talked about anvils"))
    with TestClient(app) as c:
        r = c.post("/ingest", json={"agent_id": "cc", "prompt": "hello", "response": "world"},
                   headers=_auth(MASTER))
        assert r.status_code == 200, r.text
        tenant = app.state.tenants._tenants["cc"]
        sm = tenant["sessions"]
        sm.config.hot_days = 0            # expire the session immediately
        sm._current_session_id = None     # and stop it counting as the live one
        sm._current_session_file = None
        time.sleep(0.05)
        asyncio.run(app.state.maintenance_cycle(1))

        warm = [json.loads(p.read_text(encoding="utf-8")) for p in sm.warm_dir.glob("*.json")]
        assert len(warm) == 1, "session was not archived"
        mems = [json.loads(p.read_text(encoding="utf-8"))
                for p in tenant["memory_dir"].glob("*.json")]
        mems = [m for m in mems if "archived-session" in m.get("additional_tags", [])]
        assert len(mems) == 1, "archived session did not become a session_log memory"
        assert mems[0]["timestamp"] == warm[0]["last_exchange"]
        assert mems[0]["timestamp"] != warm[0]["archived_at"]
        assert mems[0]["created_at"] > 0

        # The contract with the other half of the fix: the dreamer's gate
        # fails OPEN on a format it cannot parse, so a drift here would leave
        # both suites green and production back on the old behaviour.
        import importlib.util
        from datetime import datetime, timedelta, timezone
        from pathlib import Path as _P
        spec = importlib.util.spec_from_file_location(
            "mnemo_dream_contract", _P(__file__).resolve().parent.parent / "mnemo-dream.py")
        dream = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dream)
        ts = mems[0]["timestamp"]
        now = datetime.now(timezone.utc)
        assert dream._predates_window(ts, now + timedelta(days=2)), "server timestamp not parsed by the dreamer gate"
        assert not dream._predates_window(ts, now - timedelta(days=2))
