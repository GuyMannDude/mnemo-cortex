"""C1 (path traversal via agent_id) + C2 (fail-closed auth) regression tests.

C1: agent_id is interpolated into on-disk tenant paths. Without validation an
absolute or '..' agent_id escapes the data root (pathlib drops the left operand
on an absolute right operand). validate_agent_id must reject those, and every
tenant endpoint must return 400 rather than writing outside the data root.

C2: a stock deployment must not bind a non-loopback interface with no auth.
"""
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, CacheConfig, ClassificationConfig, ProviderConfig,
    ResilientProviderConfig, ServerConfig, DEFAULT_PERSONAS, validate_agent_id,
    validate_session_id,
)
from agentb.server import auth_posture_is_open, assert_safe_auth_posture

MASTER = "master-secret"

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


# ── C1: validate_agent_id ──

@pytest.mark.parametrize("good", ["cc", "rocky", "opie", "unknown-agent",
                                  "agent_1", "A" * 64])
def test_validate_agent_id_accepts_bare_tokens(good):
    assert validate_agent_id(good) == good


@pytest.mark.parametrize("bad", [
    "/etc/cron.d/x",          # absolute — pathlib would discard the base
    "../../../tmp/pwn",       # relative traversal
    "..",
    "a/b",                    # embedded separator
    "a\\b",                   # windows separator
    "with space",
    "",                       # empty
    "A" * 65,                 # too long
    "tab\tchar",
])
def test_validate_agent_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        validate_agent_id(bad)


# ── C1: end-to-end — traversal agent_id is refused, nothing escapes ──

@pytest.fixture
def client(tmp_path):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(),
        server=ServerConfig(port=50097, auth_token=MASTER),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_writeback_rejects_traversal_agent_id(client):
    body = {"agent_id": "../../../tmp/mnemo_pwn", "summary": "escape",
            "key_facts": ["x"], "session_id": "s1"}
    r = client.post("/writeback", json=body, headers=_auth(MASTER))
    assert r.status_code == 400
    # And nothing was written outside the data root.
    assert not Path("/tmp/mnemo_pwn").exists()


def test_writeback_rejects_absolute_agent_id(client):
    body = {"agent_id": "/tmp/mnemo_abs_pwn", "summary": "escape",
            "key_facts": ["x"], "session_id": "s1"}
    r = client.post("/writeback", json=body, headers=_auth(MASTER))
    assert r.status_code == 400
    assert not Path("/tmp/mnemo_abs_pwn").exists()


def test_context_rejects_traversal_agent_id(client):
    r = client.post("/context", json={"agent_id": "../../etc", "prompt": "hi"},
                    headers=_auth(MASTER))
    assert r.status_code == 400


def test_valid_agent_id_still_works(client):
    body = {"agent_id": "cc", "summary": "ok", "key_facts": ["x"], "session_id": "s1"}
    r = client.post("/writeback", json=body, headers=_auth(MASTER))
    assert r.status_code == 200


# ── C1-sibling: session_id traversal in get_session_transcript ──

@pytest.mark.parametrize("good", ["2026-07-06_121245_a1b2c3", "cc-2026-07-06-12-42-40",
                                  "s1", "scoped-test-1", "A" * 128])
def test_validate_session_id_accepts_real_ids(good):
    assert validate_session_id(good) == good


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd", "/etc/passwd", "a/b", "a.b", "", "A" * 129, "x\ny",
])
def test_validate_session_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        validate_session_id(bad)


def test_get_session_transcript_blocks_traversal(tmp_path):
    """The SessionManager read path must not read arbitrary *.jsonl off disk."""
    from agentb.sessions import SessionManager, SessionConfig
    # Plant a file one level above the tenant's session dirs.
    (tmp_path / "sessions").mkdir()
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"_type":"exchange","prompt":"leak"}\n')
    sm = SessionManager(tmp_path / "sessions", SessionConfig())
    with pytest.raises(ValueError):
        sm.get_session_transcript("../../secret")
    # A real ingest→retrieve round-trip still works.
    r = sm.ingest("hi", "there")
    assert sm.get_session_transcript(r["session_id"])


def test_transcript_endpoint_rejects_bad_session_id(client):
    # Dot is invalid and URL-safe, so it reaches the handler and maps to 400.
    r = client.get("/sessions/a.b/transcript", headers=_auth(MASTER))
    assert r.status_code == 400


# ── C2: fail-closed auth posture ──

def _cfg(host, auth_token="", allow=False):
    return AgentBConfig(
        server=ServerConfig(host=host, auth_token=auth_token, allow_unauthenticated=allow),
        personas=dict(DEFAULT_PERSONAS),
    )


def test_open_posture_detected_for_public_bind_no_auth():
    assert auth_posture_is_open(_cfg("0.0.0.0")) is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_safe(host):
    assert auth_posture_is_open(_cfg(host)) is False


def test_auth_token_makes_posture_safe():
    assert auth_posture_is_open(_cfg("0.0.0.0", auth_token="secret")) is False


def test_explicit_opt_in_makes_posture_safe():
    assert auth_posture_is_open(_cfg("0.0.0.0", allow=True)) is False


def test_assert_raises_on_open_posture():
    with pytest.raises(RuntimeError, match="Refusing to start"):
        assert_safe_auth_posture(_cfg("0.0.0.0"))


@pytest.mark.parametrize("cfg", [
    _cfg("127.0.0.1"),
    _cfg("0.0.0.0", auth_token="secret"),
    _cfg("0.0.0.0", allow=True),
])
def test_assert_passes_on_safe_posture(cfg):
    assert_safe_auth_posture(cfg)  # must not raise


def test_open_posture_refuses_app_startup(tmp_path):
    """The guard must fire on the real serving path (lifespan startup), which
    runs under uvicorn/gunicorn too — not only in __main__. Entering the
    TestClient context triggers lifespan; an open-posture config must raise."""
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(),
        server=ServerConfig(host="0.0.0.0", port=50096),  # open: public bind, no auth
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with pytest.raises(RuntimeError, match="Refusing to start"):
            with TestClient(create_app(cfg)):
                pass
