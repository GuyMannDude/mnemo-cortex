"""GHSA-682p-grrx-wrwg regression: cross-origin access to the unauthenticated
loopback server.

A browser on the same machine is a loopback client, so the loopback peer check
alone does not stop a web page from driving the API. The stock config must not
answer cross-origin requests with a wildcard CORS header, and an
unauthenticated server must refuse requests carrying a foreign Origin, even
for "simple" requests that skip the CORS preflight.
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, CacheConfig, ClassificationConfig, ProviderConfig,
    ResilientProviderConfig, ServerConfig, DEFAULT_PERSONAS,
)

from tests.test_security_c1_c2 import FakeEmbedding, FakeReasoning

EVIL = "https://evil.example"


def _app(tmp_path, **server):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50095, **server),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        return create_app(cfg)


@pytest.fixture
def local(tmp_path):
    with TestClient(_app(tmp_path), client=("127.0.0.1", 50000)) as c:
        yield c


def test_default_cors_origins_is_not_wildcard():
    assert "*" not in ServerConfig().cors_origins


def test_load_config_without_cors_key_is_not_wildcard(tmp_path):
    from agentb.config import load_config
    p = tmp_path / "agentb.yaml"
    p.write_text("server:\n  host: 127.0.0.1\n")
    assert "*" not in load_config(str(p)).server.cors_origins


def test_preflight_from_foreign_origin_gets_no_allow_origin(local):
    r = local.options("/facts", headers={
        "Origin": EVIL, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_foreign_origin_cannot_read_unauthenticated(local):
    r = local.get("/health", headers={"Origin": EVIL})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


def test_foreign_origin_cannot_pause_capture(local):
    # text/plain POST is a CORS "simple" request: the browser sends it with no
    # preflight, so the server itself must refuse it.
    r = local.post("/capture/pause", content="{}",
                   headers={"Origin": EVIL, "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert local.get("/capture/status").json().get("paused") is not True


@pytest.mark.parametrize("origin", ["null", "http://[x", "http://127.0.0.1.evil.example"])
def test_odd_origin_is_refused(local, origin):
    assert local.get("/health", headers={"Origin": origin}).status_code == 403


def test_explicit_wildcard_does_not_reopen_unauthenticated_server(tmp_path):
    # Existing YAML copied from the old example still says ["*"].
    with TestClient(_app(tmp_path, cors_origins=["*"]), client=("127.0.0.1", 50000)) as c:
        assert c.get("/health", headers={"Origin": EVIL}).status_code == 403


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:50095", "http://localhost:50095", "http://[::1]:50095"])
def test_loopback_origin_still_allowed(local, origin):
    assert local.get("/health", headers={"Origin": origin}).status_code == 200


def test_no_origin_header_still_allowed(local):
    # CLI, MCP and subprocess clients send no Origin header.
    assert local.get("/health").status_code == 200


def test_listed_origin_allowed(tmp_path):
    with TestClient(_app(tmp_path, cors_origins=["https://ui.example"]),
                    client=("127.0.0.1", 50000)) as c:
        r = c.get("/health", headers={"Origin": "https://ui.example"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://ui.example"


def test_token_auth_still_gates_foreign_origin(tmp_path):
    # With auth configured the token is the gate; a browser page cannot attach
    # it, so the Origin guard is not needed there.
    with TestClient(_app(tmp_path, auth_token="t0k"), client=("127.0.0.1", 50000)) as c:
        assert c.get("/sessions", headers={"Origin": EVIL}).status_code == 401
        assert c.get("/sessions", headers={"Origin": EVIL,
                                           "X-API-KEY": "t0k"}).status_code == 200


@pytest.mark.parametrize("host", ["rebind.example:50095", "127.0.0.1.evil.example", "[x"])
def test_dns_rebinding_host_is_refused(local, host):
    # After rebinding, the page's same-origin GET carries no Origin header,
    # only the attacker's name in Host.
    assert local.get("/facts", headers={"Host": host}).status_code == 403
    r = local.post("/capture/pause", json={}, headers={"Host": host})
    assert r.status_code == 403
    assert local.get("/capture/status").json().get("paused") is not True


@pytest.mark.parametrize("host", [
    "127.0.0.1:50095", "localhost:50095", "[::1]:50095", "localhost", "127.0.1.1"])
def test_local_host_header_still_allowed(local, host):
    assert local.get("/health", headers={"Host": host}).status_code == 200


def test_machine_hostname_still_allowed(local):
    import socket
    assert local.get("/health", headers={"Host": socket.gethostname()}).status_code == 200


def test_token_auth_ignores_host_header(tmp_path):
    with TestClient(_app(tmp_path, auth_token="t0k"), client=("127.0.0.1", 50000)) as c:
        assert c.get("/sessions", headers={"Host": "mnemo.lan:50001",
                                           "X-API-KEY": "t0k"}).status_code == 200


def test_machine_hostname_origin_still_allowed(local):
    # A Pocket tab opened at http://<hostname>:50001/app/ posts with that
    # hostname as its Origin; the Host check already vouches for the name.
    import socket
    origin = f"http://{socket.gethostname()}:50095"
    assert local.get("/health", headers={"Origin": origin}).status_code == 200


def test_opted_open_server_still_refuses_foreign_origin(tmp_path):
    # allow_unauthenticated trusts LAN peers, not every web page they visit:
    # the Origin guard applies whenever no token is configured.
    with TestClient(_app(tmp_path, allow_unauthenticated=True),
                    client=("192.0.2.7", 50000)) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/health", headers={"Origin": EVIL}).status_code == 403
        r = c.post("/capture/pause", content="{}",
                   headers={"Origin": EVIL, "Content-Type": "text/plain"})
        assert r.status_code == 403
        assert c.get("/capture/status").json().get("paused") is not True
        # reached by name from another machine: no Host allow-list here
        assert c.get("/health", headers={"Host": "mnemo.lan:50095"}).status_code == 200
