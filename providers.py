"""
AgentB Provider Abstraction Layer v0.3.0
Pluggable backends with circuit-breaker fallback chains.
"""

import time
import logging
from abc import ABC, abstractmethod
from typing import Optional
from agentb.config import ProviderConfig, ResilientProviderConfig

log = logging.getLogger("agentb.providers")


# ─────────────────────────────────────────────
#  Base Classes
# ─────────────────────────────────────────────

class ReasoningProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @property
    def label(self) -> str:
        return f"{self.config.provider}/{self.config.model}"


class EmbeddingProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @property
    def label(self) -> str:
        return f"{self.config.provider}/{self.config.model}"


# ─────────────────────────────────────────────
#  Circuit Breaker
# ─────────────────────────────────────────────

class CircuitBreaker:
    """Tracks failures and skips unhealthy providers."""

    def __init__(self, threshold: int = 3, cooldown: float = 60.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.is_open = False

    def record_success(self):
        self.failure_count = 0
        self.is_open = False

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.threshold:
            self.is_open = True

    def should_skip(self) -> bool:
        if not self.is_open:
            return False
        elapsed = time.time() - self.last_failure_time
        if elapsed >= self.cooldown:
            self.is_open = False
            self.failure_count = 0
            return False
        return True

    @property
    def retry_in(self) -> float:
        if not self.is_open:
            return 0.0
        elapsed = time.time() - self.last_failure_time
        return max(0.0, self.cooldown - elapsed)


# ─────────────────────────────────────────────
#  Resilient Wrappers (fallback chains)
# ─────────────────────────────────────────────

class ResilientReasoning:
    """Reasoning provider with circuit-breaker fallback chain."""

    def __init__(self, config: ResilientProviderConfig):
        self.config = config
        self.primary = _create_reasoning(config.primary)
        self.fallbacks = [_create_reasoning(fb) for fb in config.fallbacks]
        self.breaker = CircuitBreaker(config.circuit_breaker_threshold, config.circuit_breaker_cooldown)
        self.active_label = self.primary.label
        self.failed_over = False

    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        # Try primary if circuit is closed
        if not self.breaker.should_skip():
            try:
                result = await self.primary.generate(prompt, system, max_tokens)
                self.breaker.record_success()
                self.active_label = self.primary.label
                self.failed_over = False
                return result
            except Exception as e:
                self.breaker.record_failure()
                log.warning(f"Primary reasoning failed ({self.primary.label}): {e}")
        else:
            log.info(f"Primary reasoning skipped (circuit open, retry in {self.breaker.retry_in:.0f}s)")

        # Try fallbacks in order
        for i, fb in enumerate(self.fallbacks):
            try:
                result = await fb.generate(prompt, system, max_tokens)
                self.active_label = fb.label
                self.failed_over = True
                log.info(f"Reasoning served by fallback [{i}]: {fb.label}")
                return result
            except Exception as e:
                log.warning(f"Fallback [{i}] reasoning failed ({fb.label}): {e}")

        raise RuntimeError("All reasoning providers failed (primary + all fallbacks)")

    async def health_check(self) -> bool:
        return await self.primary.health_check()

    @property
    def status(self) -> dict:
        return {
            "primary": self.primary.label,
            "active": self.active_label,
            "failed_over": self.failed_over,
            "circuit_open": self.breaker.is_open,
            "primary_retry_in": f"{self.breaker.retry_in:.0f}s" if self.breaker.is_open else None,
            "fallback_count": len(self.fallbacks),
        }


class ResilientEmbedding:
    """Embedding provider with circuit-breaker fallback chain."""

    def __init__(self, config: ResilientProviderConfig):
        self.config = config
        self.primary = _create_embedding(config.primary)
        self.fallbacks = [_create_embedding(fb) for fb in config.fallbacks]
        self.breaker = CircuitBreaker(config.circuit_breaker_threshold, config.circuit_breaker_cooldown)
        self.active_label = self.primary.label
        self.failed_over = False

    async def embed(self, text: str) -> list[float]:
        if not self.breaker.should_skip():
            try:
                result = await self.primary.embed(text)
                self.breaker.record_success()
                self.active_label = self.primary.label
                self.failed_over = False
                return result
            except Exception as e:
                self.breaker.record_failure()
                log.warning(f"Primary embedding failed ({self.primary.label}): {e}")
        else:
            log.info(f"Primary embedding skipped (circuit open, retry in {self.breaker.retry_in:.0f}s)")

        for i, fb in enumerate(self.fallbacks):
            try:
                result = await fb.embed(text)
                self.active_label = fb.label
                self.failed_over = True
                log.info(f"Embedding served by fallback [{i}]: {fb.label}")
                return result
            except Exception as e:
                log.warning(f"Fallback [{i}] embedding failed ({fb.label}): {e}")

        raise RuntimeError("All embedding providers failed (primary + all fallbacks)")

    async def health_check(self) -> bool:
        return await self.primary.health_check()

    @property
    def status(self) -> dict:
        return {
            "primary": self.primary.label,
            "active": self.active_label,
            "failed_over": self.failed_over,
            "circuit_open": self.breaker.is_open,
            "primary_retry_in": f"{self.breaker.retry_in:.0f}s" if self.breaker.is_open else None,
            "fallback_count": len(self.fallbacks),
        }


# ─────────────────────────────────────────────
#  Provider Implementations
# ─────────────────────────────────────────────

class OllamaReasoning(ReasoningProvider):
    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        import httpx
        payload = {"model": self.config.model, "prompt": prompt, "stream": False,
                   "options": {"temperature": 0.3, "num_predict": max_tokens}}
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(f"{self.config.api_base}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "")

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return (await client.get(f"{self.config.api_base}/api/tags")).status_code == 200
        except Exception:
            return False


class OllamaEmbedding(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self.config.api_base}/api/embed",
                                     json={"model": self.config.model, "input": text})
            resp.raise_for_status()
            embeddings = resp.json().get("embeddings", [[]])
            return embeddings[0] if embeddings else []

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return (await client.get(f"{self.config.api_base}/api/tags")).status_code == 200
        except Exception:
            return False


class OpenAIReasoning(ReasoningProvider):
    def _url(self):
        return self.config.api_base or "https://api.openai.com/v1"

    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        import httpx
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(f"{self._url()}/chat/completions", headers=headers,
                                     json={"model": self.config.model, "messages": messages,
                                           "max_tokens": max_tokens, "temperature": 0.3})
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return (await client.get(f"{self._url()}/models",
                                         headers={"Authorization": f"Bearer {self.config.api_key}"})).status_code == 200
        except Exception:
            return False


class OpenAIEmbedding(EmbeddingProvider):
    def _url(self):
        return self.config.api_base or "https://api.openai.com/v1"

    async def embed(self, text: str) -> list[float]:
        import httpx
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self._url()}/embeddings", headers=headers,
                                     json={"model": self.config.model, "input": text})
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return (await client.get(f"{self._url()}/models",
                                         headers={"Authorization": f"Bearer {self.config.api_key}"})).status_code == 200
        except Exception:
            return False


class AnthropicReasoning(ReasoningProvider):
    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        import httpx
        headers = {"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        payload = {"model": self.config.model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        base = self.config.api_base or "https://api.anthropic.com/v1"
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(f"{base}/messages", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"] if data.get("content") else ""

    async def health_check(self) -> bool:
        return bool(self.config.api_key)


class OpenRouterReasoning(ReasoningProvider):
    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        import httpx
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/GuyMannDude/agentb", "X-Title": "AgentB"}
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        base = self.config.api_base or "https://openrouter.ai/api/v1"
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(f"{base}/chat/completions", headers=headers,
                                     json={"model": self.config.model, "messages": messages,
                                           "max_tokens": max_tokens, "temperature": 0.3})
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def health_check(self) -> bool:
        return bool(self.config.api_key)


class OpenRouterEmbedding(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        import httpx
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        base = self.config.api_base or "https://openrouter.ai/api/v1"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{base}/embeddings", headers=headers,
                                     json={"model": self.config.model, "input": text})
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]

    async def health_check(self) -> bool:
        return bool(self.config.api_key)


class GoogleReasoning(ReasoningProvider):
    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        import httpx
        base = self.config.api_base or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{self.config.model}:generateContent?key={self.config.api_key}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            candidates = resp.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                return parts[0].get("text", "") if parts else ""
            return ""

    async def health_check(self) -> bool:
        return bool(self.config.api_key)


class GoogleEmbedding(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        import httpx
        base = self.config.api_base or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{self.config.model}:embedContent?key={self.config.api_key}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json={"content": {"parts": [{"text": text}]}})
            resp.raise_for_status()
            return resp.json().get("embedding", {}).get("values", [])

    async def health_check(self) -> bool:
        return bool(self.config.api_key)


class HuggingFaceEmbedding(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        import httpx
        if self.config.api_base:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{self.config.api_base}/embed", json={"inputs": text})
                resp.raise_for_status()
                return resp.json()[0]
        else:
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            url = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{self.config.model}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json={"inputs": text}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data[0] if isinstance(data[0], list) else data

    async def health_check(self) -> bool:
        return True


# ─────────────────────────────────────────────
#  Factories
# ─────────────────────────────────────────────

REASONING_MAP = {
    "ollama": OllamaReasoning, "openai": OpenAIReasoning,
    "anthropic": AnthropicReasoning, "openrouter": OpenRouterReasoning,
    "google": GoogleReasoning,
}

EMBEDDING_MAP = {
    "ollama": OllamaEmbedding, "openai": OpenAIEmbedding,
    "huggingface": HuggingFaceEmbedding, "google": GoogleEmbedding,
    "openrouter": OpenRouterEmbedding,
}


def _create_reasoning(config: ProviderConfig) -> ReasoningProvider:
    cls = REASONING_MAP.get(config.provider)
    if not cls:
        raise ValueError(f"Unknown reasoning provider: {config.provider}")
    return cls(config)


def _create_embedding(config: ProviderConfig) -> EmbeddingProvider:
    cls = EMBEDDING_MAP.get(config.provider)
    if not cls:
        raise ValueError(f"Unknown embedding provider: {config.provider}")
    return cls(config)


def create_resilient_reasoning(config: ResilientProviderConfig) -> ResilientReasoning:
    return ResilientReasoning(config)


def create_resilient_embedding(config: ResilientProviderConfig) -> ResilientEmbedding:
    return ResilientEmbedding(config)
