"""Embedding size: hosted models are asked for the index's size, and a model
that answers a different size stops the server at startup in one plain line
instead of failing every save later with a 500."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agentb.config import (AgentBConfig, CacheConfig, ClassificationConfig, DEFAULT_PERSONAS,
                           ProviderConfig, ResilientProviderConfig, ServerConfig)
from agentb.providers import (OpenAIEmbedding, OpenRouterEmbedding, _dimensions_field,
                              probe_embedding_dim)
from agentb.vec import EMBED_DIM


def _pc(model, provider="openai", **extra):
    return ProviderConfig(provider=provider, model=model, api_key="k", extra=extra)


# ── which requests carry `dimensions` ─────────────────────────────────────

@pytest.mark.parametrize("model,extra,want", [
    ("text-embedding-3-small", {}, {"dimensions": EMBED_DIM}),
    ("text-embedding-3-large", {}, {"dimensions": EMBED_DIM}),
    ("openai/text-embedding-3-small", {}, {"dimensions": EMBED_DIM}),   # OpenRouter's name
    ("text-embedding-ada-002", {}, {}),            # rejects the field
    ("nomic-embed-text", {}, {}),                  # OpenAI-compatible local server
    ("qwen/qwen3-embedding-8b", {"dimensions": 768}, {"dimensions": 768}),
    ("text-embedding-3-small", {"dimensions": 0}, {}),   # 0 = send nothing
    ("text-embedding-3-large", {"dimensions": 1024}, {"dimensions": 1024}),
])
def test_dimensions_field(model, extra, want):
    assert _dimensions_field(_pc(model, **extra)) == want


class _Resp:
    def __init__(self, n): self._n = n
    def raise_for_status(self): pass
    def json(self): return {"data": [{"embedding": [0.1] * self._n}]}


@pytest.mark.parametrize("cls,provider", [(OpenAIEmbedding, "openai"), (OpenRouterEmbedding, "openrouter")])
def test_embedders_send_dimensions(cls, provider):
    sent = []

    async def post(self, url, *, headers=None, json=None):
        sent.append(json)
        return _Resp(EMBED_DIM)

    with patch.object(httpx.AsyncClient, "post", post):
        asyncio.run(cls(_pc("text-embedding-3-small", provider)).embed("x"))
        asyncio.run(cls(_pc("nomic-embed-text", provider)).embed("x"))
    assert sent[0] == {"model": "text-embedding-3-small", "input": "x", "dimensions": EMBED_DIM}
    assert sent[1] == {"model": "nomic-embed-text", "input": "x"}


# ── the startup check ─────────────────────────────────────────────────────

class _Emb:
    def __init__(self, n=EMBED_DIM, exc=None, delay=0.0):
        self.n, self.exc, self.delay = n, exc, delay
        self.label = "openai/text-embedding-ada-002"
        self.primary = self

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return [0.0] * self.n

    status = {"primary": "fake"}
    active_label = "fake"
    async def health_check(self): return True


def test_probe_right_size_is_fine():
    assert asyncio.run(probe_embedding_dim(_Emb())) is None


def test_probe_wrong_size_names_model_and_both_sizes():
    msg = asyncio.run(probe_embedding_dim(_Emb(n=1536)))
    assert "openai/text-embedding-ada-002 returns 1536 numbers" in msg
    assert f"index holds {EMBED_DIM}" in msg
    assert "nomic-embed-text" in msg and "text-embedding-3-small" in msg


@pytest.mark.parametrize("emb", [
    _Emb(exc=httpx.ConnectError("refused")),
    _Emb(exc=RuntimeError("boom")),
    _Emb(delay=1.0),                      # slower than the timeout below
], ids=["unreachable", "error", "timeout"])
def test_probe_outage_is_not_a_refusal(emb):
    assert asyncio.run(probe_embedding_dim(emb, timeout=0.2)) is None


def _cfg(tmp_path):
    return AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )


@pytest.mark.parametrize("n,starts", [(EMBED_DIM, True), (1536, False)])
def test_server_start(tmp_path, n, starts):
    with patch("agentb.server.create_resilient_embedding", return_value=_Emb(n=n)), \
         patch("agentb.server.create_resilient_reasoning", return_value=_Emb()):
        from agentb.server import create_app
        app = create_app(_cfg(tmp_path))
        if starts:
            with TestClient(app) as c:
                assert c.get("/health").status_code == 200
        else:
            with pytest.raises(RuntimeError, match="Refusing to start: .* returns 1536 numbers"):
                with TestClient(app):
                    pass
