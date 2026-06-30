"""
AgentB Provider Abstraction Layer v0.3.0
Pluggable backends with circuit-breaker fallback chains.
"""

import os
import time
import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from agentb.config import ProviderConfig, ResilientProviderConfig

log = logging.getLogger("agentb.providers")


class EmbeddingRefused(RuntimeError):
    """No embedder produced a valid index-dim vector, so the embed is REFUSED.

    Foundation-audit 4.5 (refuse-and-alert): on embedder outage or a wrong-dim
    fallback we refuse rather than write a mismatched/missing vector — never
    silently lose context, never corrupt the index. Subclasses RuntimeError so
    existing `except RuntimeError`/`except Exception` callers still catch it.
    """


class _EmbedAlerter:
    """Rate-limited Discord 'scream' for embedder refusal. Fail-safe: a missing
    webhook or a failed post is logged, never raised — alerting must not add a
    second failure on top of the embedder being down."""

    _COOLDOWN = 300.0  # one alert per 5 min, so a sustained outage doesn't spam

    def __init__(self):
        self._last = 0.0

    async def scream(self, msg: str):
        now = time.time()
        if now - self._last < self._COOLDOWN:
            return
        self._last = now
        url = (os.environ.get("MNEMO_ALERT_DISCORD_WEBHOOK")
               or os.environ.get("MNEMO_DREAM_DISCORD_WEBHOOK")
               or os.environ.get("DISCORD_WEBHOOK_URL"))
        if not url:
            log.error(f"[ALERT no-webhook] {msg}")
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                await c.post(url, json={"content": f"🚨 Mnemo embedder: {msg}"})
        except Exception as e:
            log.error(f"Discord embedder-alert post failed: {e}")


_alerter = _EmbedAlerter()


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

    # health_check now does a real (network) auth probe, and /health is
    # unauthenticated + monitor-polled — cache the result so we don't fire an
    # upstream probe on every hit.
    _HC_TTL = 30.0

    def __init__(self, config: ResilientProviderConfig):
        self.config = config
        self.primary = _create_reasoning(config.primary)
        self.fallbacks = [_create_reasoning(fb) for fb in config.fallbacks]
        self.breaker = CircuitBreaker(config.circuit_breaker_threshold, config.circuit_breaker_cooldown)
        self.active_label = self.primary.label
        self.failed_over = False
        self._hc_result: Optional[bool] = None
        self._hc_at = 0.0

    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048,
                       *, use_breaker: bool = True) -> str:
        # Batch callers (the nightly dreamer reclassification pass, batch
        # writebacks) pass use_breaker=False: they hit the same reasoning backend
        # as the live preflight/classify path but must NOT share its circuit-breaker
        # state. A large batch must not be able to trip the breaker (degrading live
        # preflight) or be blocked by it, and must not perturb live /health
        # reporting. (batch-vs-live breaker isolation doctrine.)
        if use_breaker and self.breaker.should_skip():
            log.info(f"Primary reasoning skipped (circuit open, retry in {self.breaker.retry_in:.0f}s)")
        else:
            try:
                result = await self.primary.generate(prompt, system, max_tokens)
                if use_breaker:
                    self.breaker.record_success()
                    self.active_label = self.primary.label
                    self.failed_over = False
                return result
            except Exception as e:
                if use_breaker:
                    self.breaker.record_failure()
                log.warning(f"Primary reasoning failed ({self.primary.label}): {e}")

        # Try fallbacks in order
        for i, fb in enumerate(self.fallbacks):
            try:
                result = await fb.generate(prompt, system, max_tokens)
                if use_breaker:
                    self.active_label = fb.label
                    self.failed_over = True
                log.info(f"Reasoning served by fallback [{i}]: {fb.label}")
                return result
            except Exception as e:
                log.warning(f"Fallback [{i}] reasoning failed ({fb.label}): {e}")

        raise RuntimeError("All reasoning providers failed (primary + all fallbacks)")

    async def health_check(self) -> bool:
        # Reports PRIMARY health (truthfully — the probe is real now), so a dead
        # or 401'ing primary surfaces as degraded instead of hiding behind the
        # fallback. TTL-cached to keep /health cheap.
        now = time.time()
        if self._hc_result is not None and (now - self._hc_at) < self._HC_TTL:
            return self._hc_result
        self._hc_result = await self.primary.health_check()
        self._hc_at = now
        return self._hc_result

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

    # Floor for adaptive halving on input-too-long. Mirrors vec.embed_with_adaptive_truncation.
    ADAPTIVE_MIN_CHARS = 500
    # See ResilientReasoning._HC_TTL — cache the real auth probe behind /health.
    _HC_TTL = 30.0

    def __init__(self, config: ResilientProviderConfig):
        self.config = config
        self.primary = _create_embedding(config.primary)
        self.fallbacks = [_create_embedding(fb) for fb in config.fallbacks]
        self.breaker = CircuitBreaker(config.circuit_breaker_threshold, config.circuit_breaker_cooldown)
        self.active_label = self.primary.label
        self.failed_over = False
        self._hc_result: Optional[bool] = None
        self._hc_at = 0.0
        # Index dimension lock (4.5): self-calibrated from the first successful
        # PRIMARY embed (the source of truth). Any later vector — primary or
        # fallback — must match it, else it's rejected so a wrong-dim fallback
        # can never reach the index. vec.EMBED_DIM is the deeper backstop.
        self._locked_dim: Optional[int] = None

    def _check_dim(self, vec: list[float], label: str, *, is_primary: bool) -> Optional[list[float]]:
        """Return `vec` if its dimension is safe for the index, else None.

        The lock self-calibrates from the first successful PRIMARY embed. Until
        then — cold start, where the primary has never succeeded (e.g. a restart
        during a primary outage) — a fallback is validated against vec's hard
        EMBED_DIM, so a wrong-dim fallback can never reach the index even before
        the lock is set."""
        n = len(vec)
        if self._locked_dim is None:
            if is_primary:
                self._locked_dim = n
                return vec
            from agentb.vec import EMBED_DIM
            if n == EMBED_DIM:
                return vec
            log.error(
                f"{label} cold-start fallback dim {n} != index dim {EMBED_DIM}; "
                f"rejecting to protect the index (4.5)"
            )
            return None
        if n == self._locked_dim:
            return vec
        log.error(
            f"{label} returned dim {n} != locked index dim {self._locked_dim}; "
            f"rejecting this vector to protect the index (4.5)"
        )
        return None

    async def _try_embed_adaptive(self, provider, text: str) -> list[float]:
        """Embed via one provider, halving input on HTTP 400 (context-length).

        An input-too-long 400 is a property of the input, not the provider —
        retrying on a different provider with the same text will fail the same
        way (and pollute the circuit breaker). Halve and retry the same
        provider until success or we hit the min_chars floor, then re-raise.
        """
        current = text
        while True:
            try:
                return await provider.embed(current)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400 and len(current) > self.ADAPTIVE_MIN_CHARS:
                    new_len = max(self.ADAPTIVE_MIN_CHARS, len(current) // 2)
                    log.warning(
                        f"Embed 400 at {len(current)} chars on {provider.label}; "
                        f"halving to {new_len} (input too long, not a provider failure)"
                    )
                    current = current[:new_len]
                    continue
                raise

    async def embed(self, text: str, *, use_breaker: bool = True) -> list[float]:
        # Batch callers (e.g. the nightly dreamer) pass use_breaker=False: they
        # hit the same embedding backend as the live /context path but must NOT
        # share its circuit-breaker failure state. A large batch must not be
        # able to trip the breaker (poisoning live reads) or be blocked by it,
        # and its provider-health flags must not perturb live /health reporting.
        # (batch-vs-live breaker isolation doctrine.)
        if use_breaker and self.breaker.should_skip():
            log.info(f"Primary embedding skipped (circuit open, retry in {self.breaker.retry_in:.0f}s)")
        else:
            try:
                result = await self._try_embed_adaptive(self.primary, text)
                checked = self._check_dim(result, self.primary.label, is_primary=True)
                if checked is not None:
                    if use_breaker:
                        self.breaker.record_success()
                        self.active_label = self.primary.label
                        self.failed_over = False
                    return checked
                # Wrong-dim primary = misconfig, not an outage — don't trip the
                # breaker; fall through to fallbacks, then refuse.
            except Exception as e:
                if use_breaker:
                    self.breaker.record_failure()
                log.warning(f"Primary embedding failed ({self.primary.label}): {e}")

        for i, fb in enumerate(self.fallbacks):
            try:
                result = await self._try_embed_adaptive(fb, text)
                checked = self._check_dim(result, fb.label, is_primary=False)
                if checked is None:
                    continue  # wrong-dim fallback rejected — would corrupt the index
                if use_breaker:
                    self.active_label = fb.label
                    self.failed_over = True
                log.info(f"Embedding served by fallback [{i}]: {fb.label}")
                return checked
            except Exception as e:
                log.warning(f"Fallback [{i}] embedding failed ({fb.label}): {e}")

        # 4.5 refuse-and-alert: nothing produced a valid index-dim vector. Scream
        # on Discord and refuse — never write a missing/wrong-dim vector.
        await _alerter.scream(
            f"no embedder produced a valid {self._locked_dim or '?'}-dim vector — REFUSING new "
            f"saves to protect the index (primary={self.primary.label}, {len(self.fallbacks)} fallback(s)). "
            "Recall + writes will error until the embedder is back."
        )
        raise EmbeddingRefused(
            f"No embedder produced a valid index-dim vector "
            f"(primary={self.primary.label} + {len(self.fallbacks)} fallback(s)); save refused."
        )

    async def health_check(self) -> bool:
        # Reports PRIMARY health truthfully (real probe), TTL-cached for /health.
        now = time.time()
        if self._hc_result is not None and (now - self._hc_at) < self._HC_TTL:
            return self._hc_result
        self._hc_result = await self.primary.health_check()
        self._hc_at = now
        return self._hc_result

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
        # Real auth probe (v4.1) — GET /models is free and 401s on a bad key.
        # bool(api_key) was the Session-73 trap: a key STRING is not a working
        # key. Fail closed on any error so a dead primary screams as degraded
        # instead of hiding behind the fallback.
        if not self.config.api_key:
            return False
        base = self.config.api_base or "https://api.anthropic.com/v1"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{base}/models",
                    headers={"x-api-key": self.config.api_key,
                             "anthropic-version": "2023-06-01"})
            return resp.status_code == 200
        except Exception as e:
            log.warning(f"Anthropic auth probe failed: {e}")
            return False


async def _google_auth_ok(config: ProviderConfig) -> bool:
    """Real auth probe for a Google AI key: GET /models is free and rejects a
    bad key. Fail closed (see _openrouter_auth_ok for the rationale)."""
    if not config.api_key:
        return False
    base = config.api_base or "https://generativelanguage.googleapis.com/v1beta"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/models",
                                    params={"key": config.api_key, "pageSize": 1})
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Google auth probe failed: {e}")
        return False


async def _openrouter_auth_ok(config: ProviderConfig) -> bool:
    """Real auth probe for an OpenRouter key: GET /key → 200 iff the key
    authenticates. A non-empty key STRING is not proof it works — that was the
    Session-73 trap: health_check returned bool(api_key), so /health reported
    `reasoning healthy/openrouter` while every real call 401'd and silently failed
    over to ollama, and a transient 401 looked like a dead key for hours. The /key
    endpoint is credit-free, so probing it is cheap. Any non-200 or network error
    → unhealthy (fail closed, so the problem screams instead of hiding)."""
    if not config.api_key:
        return False
    base = config.api_base or "https://openrouter.ai/api/v1"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/key",
                                    headers={"Authorization": f"Bearer {config.api_key}"})
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"OpenRouter auth probe failed: {e}")
        return False


class OpenRouterReasoning(ReasoningProvider):
    async def generate(self, prompt: str, system: str = "", max_tokens: int = 2048) -> str:
        import httpx
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/GuyMannDude/mnemo-cortex", "X-Title": "Mnemo Cortex"}
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
        return await _openrouter_auth_ok(self.config)


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
        return await _openrouter_auth_ok(self.config)


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
        return await _google_auth_ok(self.config)


class GoogleEmbedding(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        import httpx
        base = self.config.api_base or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{self.config.model}:embedContent?key={self.config.api_key}"
        payload: dict = {"content": {"parts": [{"text": text}]}}
        # Matryoshka truncation: gemini-embedding-* models output 3072 dims natively
        # but support outputDimensionality to truncate. Set extra.output_dimensionality
        # in config to match the consumer's vec store width (e.g. 768 for nomic compat).
        if (od := self.config.extra.get("output_dimensionality")):
            payload["outputDimensionality"] = int(od)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json().get("embedding", {}).get("values", [])

    async def health_check(self) -> bool:
        return await _google_auth_ok(self.config)


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
        # Self-hosted TEI endpoint: probe /health (free). Hosted HF inference:
        # whoami-v2 validates the token without running a model. Fail closed.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if self.config.api_base:
                    resp = await client.get(f"{self.config.api_base}/health")
                    return resp.status_code == 200
                if not self.config.api_key:
                    return False
                resp = await client.get(
                    "https://huggingface.co/api/whoami-v2",
                    headers={"Authorization": f"Bearer {self.config.api_key}"})
                return resp.status_code == 200
        except Exception as e:
            log.warning(f"HuggingFace auth probe failed: {e}")
            return False


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
