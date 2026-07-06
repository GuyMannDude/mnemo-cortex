"""GET /dream/latest — the dream brief served from the Cortex host (v4.9.3).

The bridge historically read DREAM_DIR from its own local disk, which broke
silently once the dreamer ran on a different machine than the agents. These
tests pin the server-side contract the bridge's HTTP-first path relies on:
newest YYYY-MM-DD.md by filename, non-md files ignored, 404 when none exist.
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
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "x"
    async def health_check(self): return True


@pytest.fixture
def make_client(tmp_path):
    def _make():
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
            return TestClient(create_app(cfg))
    return _make


def test_404_when_no_dreams(make_client):
    with make_client() as client:
        r = client.get("/dream/latest")
        assert r.status_code == 404


def test_serves_newest_by_filename(tmp_path, make_client):
    dreams = tmp_path / "dreams"
    dreams.mkdir()
    (dreams / "2026-07-04.md").write_text("older brief", encoding="utf-8")
    (dreams / "2026-07-05.md").write_text("newest brief", encoding="utf-8")
    with make_client() as client:
        r = client.get("/dream/latest")
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2026-07-05"
        assert body["content"] == "newest brief"
        assert body["age_hours"] >= 0


def test_ignores_non_md_files(tmp_path, make_client):
    dreams = tmp_path / "dreams"
    dreams.mkdir()
    (dreams / "cron.log").write_text("noise", encoding="utf-8")
    with make_client() as client:
        r = client.get("/dream/latest")
        assert r.status_code == 404
