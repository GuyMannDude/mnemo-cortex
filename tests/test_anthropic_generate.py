"""AnthropicReasoning.generate must return the answer text even when the
reply starts with a thinking block (Claude Sonnet 5.5 and Haiku 5.5 think by
default). It used to read content[0]["text"], which raises KeyError there."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from agentb.config import ProviderConfig
from agentb.providers import AnthropicReasoning


class _FakePostResp:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): return None
    def json(self): return self._payload


class _FakePostClient:
    """Stands in for httpx.AsyncClient(...): .post returns a fixed JSON body."""
    def __init__(self, payload): self._payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, *a, json=None, headers=None, **k):
        return _FakePostResp(self._payload)


def _provider():
    return AnthropicReasoning(ProviderConfig(
        provider="anthropic", model="claude-sonnet-5-5", api_key="sk-ant-test"))


@pytest.mark.asyncio
async def test_generate_skips_thinking_blocks():
    payload = {"content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
    ]}
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakePostClient(payload)):
        assert await _provider().generate("hi") == "hello world"


@pytest.mark.asyncio
async def test_generate_without_content_returns_empty():
    with patch("agentb.providers.httpx.AsyncClient", return_value=_FakePostClient({"content": []})):
        assert await _provider().generate("hi") == ""
