"""v4.0.3 regression test: health_check must do a REAL OpenRouter auth probe.

Before the fix, OpenRouterReasoning/Embedding.health_check returned
bool(api_key) — a non-empty key string, never an actual auth check. So /health
reported `reasoning healthy/openrouter` while every real call 401'd and silently
failed over to ollama, and a transient 401 looked like a dead key for hours
(Session 73 incident). The fix hits the credit-free /key endpoint (200 == valid),
TTL-cached in the Resilient wrappers so unauthenticated /health polling doesn't
hammer OpenRouter.
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import AsyncMock, patch

from agentb.config import ResilientProviderConfig, ProviderConfig
from agentb.providers import (
    _openrouter_auth_ok, OpenRouterReasoning, OpenRouterEmbedding,
    create_resilient_reasoning,
)


class _FakeResp:
    def __init__(self, code): self.status_code = code


class _FakeClient:
    """Stands in for httpx.AsyncClient(...) — async context manager whose .get
    returns a fixed status code, or raises a network error. Records the last
    call so tests can assert the probe hit /key with the Bearer header."""
    last_url = None
    last_headers = None
    def __init__(self, code=200, raise_exc=None):
        self._code, self._raise = code, raise_exc
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, *a, headers=None, **k):
        _FakeClient.last_url, _FakeClient.last_headers = url, headers
        if self._raise:
            raise self._raise
        return _FakeResp(self._code)


def _cfg(api_key="sk-or-v1-test"):
    return ProviderConfig(provider="openrouter", model="x", api_key=api_key)


@pytest.mark.asyncio
async def test_auth_probe_200_is_healthy_and_hits_key_with_bearer():
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(200)):
        assert await _openrouter_auth_ok(_cfg("sk-or-v1-abc")) is True
    # Contract: probe the /key endpoint, authenticated.
    assert _FakeClient.last_url.endswith("/key")
    assert _FakeClient.last_headers["Authorization"] == "Bearer sk-or-v1-abc"


@pytest.mark.asyncio
async def test_auth_probe_401_is_unhealthy():
    # The exact failure that masqueraded as a dead key.
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(401)):
        assert await _openrouter_auth_ok(_cfg()) is False


@pytest.mark.asyncio
async def test_empty_key_is_unhealthy_without_network():
    # No key → False, and the probe must not even attempt a request.
    with patch("agentb.providers.httpx.AsyncClient", side_effect=AssertionError("should not call")):
        assert await _openrouter_auth_ok(_cfg(api_key="")) is False


@pytest.mark.asyncio
async def test_network_error_fails_closed():
    with patch("agentb.providers.httpx.AsyncClient",
               return_value=_FakeClient(raise_exc=ConnectionError("boom"))):
        assert await _openrouter_auth_ok(_cfg()) is False


@pytest.mark.asyncio
async def test_provider_health_checks_delegate_to_probe():
    rea = OpenRouterReasoning(_cfg())
    emb = OpenRouterEmbedding(_cfg())
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(401)):
        assert await rea.health_check() is False
        assert await emb.health_check() is False
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(200)):
        assert await rea.health_check() is True


@pytest.mark.asyncio
async def test_resilient_health_check_is_ttl_cached():
    cfg = ResilientProviderConfig(primary=_cfg())
    r = create_resilient_reasoning(cfg)
    r.primary.health_check = AsyncMock(return_value=True)

    assert await r.health_check() is True
    assert await r.health_check() is True
    assert r.primary.health_check.call_count == 1   # 2nd hit served from cache

    # Expire the cache → re-probes.
    r._hc_at = time.time() - (r._HC_TTL + 1)
    assert await r.health_check() is True
    assert r.primary.health_check.call_count == 2


# ── v4.1: the remaining bool(api_key) liars got real probes too ──

from agentb.providers import (
    AnthropicReasoning, GoogleReasoning, GoogleEmbedding,
    HuggingFaceEmbedding, _google_auth_ok,
)


class _FakeParamClient(_FakeClient):
    """Also records query params (Google probes auth via ?key=)."""
    last_params = None
    async def get(self, url, *a, headers=None, params=None, **k):
        _FakeParamClient.last_params = params
        return await super().get(url, headers=headers)


@pytest.mark.asyncio
async def test_anthropic_probe_is_real_and_fail_closed():
    cfg = ProviderConfig(provider="anthropic", model="x", api_key="sk-ant-test")
    p = AnthropicReasoning(cfg)
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(200)):
        assert await p.health_check() is True
    assert _FakeClient.last_url.endswith("/models")
    assert _FakeClient.last_headers["x-api-key"] == "sk-ant-test"
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(401)):
        assert await p.health_check() is False
    with patch("agentb.providers.httpx.AsyncClient",
               return_value=_FakeClient(raise_exc=OSError("net down"))):
        assert await p.health_check() is False
    assert await AnthropicReasoning(
        ProviderConfig(provider="anthropic", model="x", api_key="")).health_check() is False


@pytest.mark.asyncio
async def test_google_probe_is_real_and_fail_closed():
    cfg = ProviderConfig(provider="google", model="gemini", api_key="AIza-test")
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeParamClient(200)):
        assert await _google_auth_ok(cfg) is True
        assert await GoogleReasoning(cfg).health_check() is True
        assert await GoogleEmbedding(cfg).health_check() is True
    assert _FakeParamClient.last_url.endswith("/models")
    assert _FakeParamClient.last_params["key"] == "AIza-test"
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeParamClient(400)):
        assert await _google_auth_ok(cfg) is False
    assert await _google_auth_ok(
        ProviderConfig(provider="google", model="x", api_key="")) is False


@pytest.mark.asyncio
async def test_huggingface_probe_hosted_and_self_hosted():
    hosted = HuggingFaceEmbedding(
        ProviderConfig(provider="huggingface", model="x", api_key="hf_test"))
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(200)):
        assert await hosted.health_check() is True
    assert "whoami-v2" in _FakeClient.last_url
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(401)):
        assert await hosted.health_check() is False

    tei = HuggingFaceEmbedding(
        ProviderConfig(provider="huggingface", model="x", api_base="http://tei:8080"))
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakeClient(200)):
        assert await tei.health_check() is True
    assert _FakeClient.last_url == "http://tei:8080/health"
    # No key, no api_base → fail closed, not "True because hosted is free"
    bare = HuggingFaceEmbedding(ProviderConfig(provider="huggingface", model="x"))
    assert await bare.health_check() is False
