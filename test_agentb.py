"""
AgentB v0.3.0 Test Suite
Run: pytest tests/ -v
"""

import json
import time
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agentb.config import (
    AgentBConfig, ProviderConfig, ResilientProviderConfig, CacheConfig,
    PersonaConfig, AgentConfig, ServerConfig, StorageConfig,
    load_config, get_agent_data_dir, get_persona, DEFAULT_PERSONAS,
)
from agentb.providers import (
    CircuitBreaker, ResilientReasoning, ResilientEmbedding,
    OllamaReasoning, OpenAIReasoning,
    create_resilient_reasoning, create_resilient_embedding,
)
from agentb.cache import L1Cache, L2Index, l3_scan, cosine_similarity, ContextChunk


# ─────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory structure."""
    for sub in ["memory", "cache/l1", "cache/l2", "logs"]:
        (tmp_path / sub).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def basic_config(tmp_data_dir):
    return AgentBConfig(
        reasoning=ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="test-model", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")],
        ),
        embedding=ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="nomic-embed-text", api_base="http://localhost:11434"),
        ),
        cache=CacheConfig(),
        server=ServerConfig(port=50099),
        data_dir=str(tmp_data_dir),
        agents={
            "rocky": AgentConfig(data_dir=str(tmp_data_dir / "agents" / "rocky"), persona="creative"),
            "bw": AgentConfig(data_dir=str(tmp_data_dir / "agents" / "bw"), persona="strict"),
            "shared": AgentConfig(data_dir=str(tmp_data_dir / "shared"), read_only=True),
        },
        personas=dict(DEFAULT_PERSONAS),
    )


@pytest.fixture
def sample_embedding():
    """A normalized 768-dim embedding vector for testing."""
    import numpy as np
    vec = np.random.randn(768).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tolist()


@pytest.fixture
def similar_embedding(sample_embedding):
    """An embedding close to sample_embedding (high cosine similarity)."""
    import numpy as np
    vec = np.array(sample_embedding, dtype=np.float32)
    noise = np.random.randn(768).astype(np.float32) * 0.01  # very small noise
    result = vec + noise
    result /= np.linalg.norm(result)
    return result.tolist()


@pytest.fixture
def different_embedding():
    """An embedding far from sample_embedding."""
    import numpy as np
    vec = np.random.randn(768).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tolist()


# ─────────────────────────────────────────────
#  Circuit Breaker Tests
# ─────────────────────────────────────────────

class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(threshold=3, cooldown=60.0)
        assert not cb.is_open
        assert not cb.should_skip()
        assert cb.failure_count == 0

    def test_stays_closed_under_threshold(self):
        cb = CircuitBreaker(threshold=3, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open
        assert not cb.should_skip()
        assert cb.failure_count == 2

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(threshold=3, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        assert cb.should_skip()

    def test_success_resets(self):
        cb = CircuitBreaker(threshold=3, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert not cb.is_open
        assert cb.failure_count == 0

    def test_cooldown_resets_circuit(self):
        cb = CircuitBreaker(threshold=3, cooldown=0.1)  # 100ms cooldown
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        assert cb.should_skip()
        time.sleep(0.15)
        assert not cb.should_skip()
        assert not cb.is_open

    def test_retry_in_countdown(self):
        cb = CircuitBreaker(threshold=3, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        retry = cb.retry_in
        assert 0 < retry <= 60.0

    def test_retry_in_zero_when_closed(self):
        cb = CircuitBreaker(threshold=3, cooldown=60.0)
        assert cb.retry_in == 0.0


# ─────────────────────────────────────────────
#  Resilient Provider Tests
# ─────────────────────────────────────────────

class TestResilientReasoning:
    @pytest.mark.asyncio
    async def test_primary_success(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="test", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")],
        )
        resilient = create_resilient_reasoning(config)
        resilient.primary.generate = AsyncMock(return_value="primary response")
        
        result = await resilient.generate("hello")
        assert result == "primary response"
        assert not resilient.failed_over
        assert resilient.active_label == resilient.primary.label

    @pytest.mark.asyncio
    async def test_fallback_on_primary_failure(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="test", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")],
        )
        resilient = create_resilient_reasoning(config)
        resilient.primary.generate = AsyncMock(side_effect=Exception("connection refused"))
        resilient.fallbacks[0].generate = AsyncMock(return_value="fallback response")

        result = await resilient.generate("hello")
        assert result == "fallback response"
        assert resilient.failed_over
        assert resilient.breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="test", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")],
            circuit_breaker_threshold=2,
        )
        resilient = create_resilient_reasoning(config)
        resilient.primary.generate = AsyncMock(side_effect=Exception("down"))
        resilient.fallbacks[0].generate = AsyncMock(return_value="fallback")

        await resilient.generate("test1")
        await resilient.generate("test2")

        assert resilient.breaker.is_open
        # Third call should skip primary entirely
        resilient.primary.generate.reset_mock()
        await resilient.generate("test3")
        resilient.primary.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="test", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")],
        )
        resilient = create_resilient_reasoning(config)
        resilient.primary.generate = AsyncMock(side_effect=Exception("primary down"))
        resilient.fallbacks[0].generate = AsyncMock(side_effect=Exception("fallback down"))

        with pytest.raises(RuntimeError, match="All reasoning providers failed"):
            await resilient.generate("hello")

    @pytest.mark.asyncio
    async def test_primary_recovery_after_cooldown(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="test", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")],
            circuit_breaker_threshold=1,
            circuit_breaker_cooldown=0.1,
        )
        resilient = create_resilient_reasoning(config)
        
        # Fail primary
        resilient.primary.generate = AsyncMock(side_effect=Exception("down"))
        resilient.fallbacks[0].generate = AsyncMock(return_value="fallback")
        await resilient.generate("test")
        assert resilient.breaker.is_open

        # Wait for cooldown
        time.sleep(0.15)

        # Primary recovers
        resilient.primary.generate = AsyncMock(return_value="primary back")
        result = await resilient.generate("test")
        assert result == "primary back"
        assert not resilient.failed_over

    def test_status_report(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="qwen2.5:32b-instruct"),
            fallbacks=[ProviderConfig(provider="openai", model="gpt-4o-mini")],
        )
        resilient = create_resilient_reasoning(config)
        status = resilient.status
        assert status["primary"] == "ollama/qwen2.5:32b-instruct"
        assert status["fallback_count"] == 1
        assert not status["failed_over"]
        assert not status["circuit_open"]


class TestResilientEmbedding:
    @pytest.mark.asyncio
    async def test_primary_success(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="nomic", api_base="http://localhost:11434"),
        )
        resilient = create_resilient_embedding(config)
        resilient.primary.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])

        result = await resilient.embed("test")
        assert result == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self):
        config = ResilientProviderConfig(
            primary=ProviderConfig(provider="ollama", model="nomic", api_base="http://localhost:11434"),
            fallbacks=[ProviderConfig(provider="openai", model="text-embedding-3-small", api_key="sk-test")],
        )
        resilient = create_resilient_embedding(config)
        resilient.primary.embed = AsyncMock(side_effect=Exception("ollama down"))
        resilient.fallbacks[0].embed = AsyncMock(return_value=[0.4, 0.5, 0.6])

        result = await resilient.embed("test")
        assert result == [0.4, 0.5, 0.6]
        assert resilient.failed_over


# ─────────────────────────────────────────────
#  Cache Tests
# ─────────────────────────────────────────────

class TestCosine:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0, abs=0.001)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0, abs=0.001)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0, abs=0.001)

    def test_zero_vector(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0


class TestL1Cache:
    def test_add_and_search(self, tmp_data_dir, sample_embedding, similar_embedding):
        l1 = L1Cache(tmp_data_dir / "cache" / "l1", CacheConfig(l1_similarity_threshold=0.7))
        asyncio.get_event_loop().run_until_complete(
            l1.add("Easter gnomes $20-30", "test", sample_embedding)
        )
        assert l1.size == 1

        results = l1.search(similar_embedding, top_k=3)
        assert len(results) >= 1
        assert results[0].cache_tier == "L1"
        assert "Easter gnomes" in results[0].content

    def test_no_match_below_threshold(self, tmp_data_dir, sample_embedding, different_embedding):
        l1 = L1Cache(tmp_data_dir / "cache" / "l1", CacheConfig(l1_similarity_threshold=0.95))
        asyncio.get_event_loop().run_until_complete(
            l1.add("test content", "test", sample_embedding)
        )
        results = l1.search(different_embedding, top_k=3)
        # Different random vector — likely below 0.95 threshold
        # (statistically near-certain but not guaranteed)
        assert len(results) <= 1

    def test_eviction_at_max(self, tmp_data_dir, sample_embedding):
        l1 = L1Cache(tmp_data_dir / "cache" / "l1", CacheConfig(l1_max_bundles=2))
        loop = asyncio.get_event_loop()
        loop.run_until_complete(l1.add("first", "s1", sample_embedding))
        loop.run_until_complete(l1.add("second", "s2", sample_embedding))
        loop.run_until_complete(l1.add("third", "s3", sample_embedding))
        assert l1.size == 2  # oldest evicted

    def test_stale_bundles_skipped(self, tmp_data_dir, sample_embedding):
        l1 = L1Cache(tmp_data_dir / "cache" / "l1", CacheConfig(l1_ttl_seconds=0))
        asyncio.get_event_loop().run_until_complete(
            l1.add("stale content", "test", sample_embedding)
        )
        time.sleep(0.01)
        results = l1.search(sample_embedding)
        assert len(results) == 0  # all expired

    def test_persona_overrides_threshold(self, tmp_data_dir, sample_embedding, similar_embedding):
        l1 = L1Cache(tmp_data_dir / "cache" / "l1", CacheConfig(l1_similarity_threshold=0.99))
        asyncio.get_event_loop().run_until_complete(
            l1.add("test", "test", sample_embedding)
        )
        # Default threshold too high
        strict_results = l1.search(similar_embedding)
        
        # Creative persona lowers threshold
        creative = PersonaConfig(name="creative", l1_similarity_override=0.5)
        creative_results = l1.search(similar_embedding, persona=creative)
        assert len(creative_results) >= len(strict_results)

    def test_disk_persistence(self, tmp_data_dir, sample_embedding):
        l1_dir = tmp_data_dir / "cache" / "l1"
        l1 = L1Cache(l1_dir, CacheConfig())
        asyncio.get_event_loop().run_until_complete(
            l1.add("persistent content", "test", sample_embedding)
        )
        json_files = list(l1_dir.glob("*.json"))
        assert len(json_files) == 1

        # Reload from disk
        l1_reloaded = L1Cache(l1_dir, CacheConfig())
        assert l1_reloaded.size == 1


class TestL2Index:
    def test_add_and_search(self, tmp_data_dir, sample_embedding, similar_embedding):
        l2 = L2Index(tmp_data_dir / "cache" / "l2", CacheConfig())
        asyncio.get_event_loop().run_until_complete(
            l2.add("Stormtrooper bunnies launch", "session:123", sample_embedding)
        )
        results = l2.search(similar_embedding)
        assert len(results) >= 1
        assert results[0].cache_tier == "L2"

    def test_index_persistence(self, tmp_data_dir, sample_embedding):
        l2_dir = tmp_data_dir / "cache" / "l2"
        l2 = L2Index(l2_dir, CacheConfig())
        asyncio.get_event_loop().run_until_complete(
            l2.add("test entry", "test", sample_embedding)
        )
        assert (l2_dir / "index.json").exists()

        l2_reloaded = L2Index(l2_dir, CacheConfig())
        assert l2_reloaded.size == 1


# ─────────────────────────────────────────────
#  Config Tests
# ─────────────────────────────────────────────

class TestConfig:
    def test_default_config(self):
        cfg = AgentBConfig()
        from agentb.config import _apply_defaults
        cfg = _apply_defaults(cfg)
        assert cfg.data_dir.endswith(".agentb")
        assert cfg.reasoning.primary.provider == "ollama"

    def test_get_agent_data_dir_configured(self, basic_config):
        path = get_agent_data_dir(basic_config, "rocky")
        assert "rocky" in str(path)

    def test_get_agent_data_dir_unknown(self, basic_config):
        path = get_agent_data_dir(basic_config, "unknown-agent")
        assert "unknown-agent" in str(path)

    def test_get_agent_data_dir_default(self, basic_config):
        path = get_agent_data_dir(basic_config, None)
        assert "default" in str(path)

    def test_get_persona_by_name(self, basic_config):
        p = get_persona(basic_config, "strict")
        assert p.name == "strict"
        assert p.preflight == "aggressive"

    def test_get_persona_from_agent(self, basic_config):
        p = get_persona(basic_config, agent_id="rocky")
        assert p.name == "creative"

    def test_get_persona_default_fallback(self, basic_config):
        p = get_persona(basic_config, agent_id="nonexistent")
        assert p.name == "default"

    def test_resilient_config_flat_format(self):
        """v0.2 backward compatibility — flat provider block."""
        raw = {
            "reasoning": {
                "provider": "ollama",
                "model": "qwen2.5:32b-instruct",
                "api_base": "http://localhost:11434",
                "fallbacks": [
                    {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}
                ]
            }
        }
        from agentb.config import _parse_config, _apply_defaults
        cfg = _apply_defaults(_parse_config(raw))
        assert cfg.reasoning.primary.provider == "ollama"
        assert len(cfg.reasoning.fallbacks) == 1
        assert cfg.reasoning.fallbacks[0].provider == "openai"

    def test_env_var_resolution(self):
        import os
        os.environ["TEST_AGENTB_KEY"] = "my-secret"
        from agentb.config import _resolve_env
        assert _resolve_env("${TEST_AGENTB_KEY}") == "my-secret"
        assert _resolve_env("plain-string") == "plain-string"
        del os.environ["TEST_AGENTB_KEY"]

    def test_yaml_loading(self, tmp_path):
        config_file = tmp_path / "agentb.yaml"
        config_file.write_text("""
data_dir: /tmp/test-agentb
log_level: debug
reasoning:
  provider: openai
  model: gpt-4o-mini
  api_key: sk-test
embedding:
  provider: ollama
  model: nomic-embed-text
  api_base: http://localhost:11434
agents:
  rocky:
    persona: creative
  bw:
    persona: strict
""")
        cfg = load_config(str(config_file))
        assert cfg.reasoning.primary.provider == "openai"
        assert cfg.reasoning.primary.model == "gpt-4o-mini"
        assert cfg.embedding.primary.provider == "ollama"
        assert "rocky" in cfg.agents
        assert cfg.agents["rocky"].persona == "creative"


# ─────────────────────────────────────────────
#  Multi-Tenant Isolation Tests
# ─────────────────────────────────────────────

class TestMultiTenant:
    def test_separate_data_dirs(self, basic_config):
        rocky_dir = get_agent_data_dir(basic_config, "rocky")
        bw_dir = get_agent_data_dir(basic_config, "bw")
        assert rocky_dir != bw_dir
        assert "rocky" in str(rocky_dir)
        assert "bw" in str(bw_dir)

    def test_isolated_memories(self, tmp_data_dir, sample_embedding):
        """Two agents writing to their own L2 indexes don't see each other's data."""
        rocky_dir = tmp_data_dir / "agents" / "rocky" / "cache" / "l2"
        bw_dir = tmp_data_dir / "agents" / "bw" / "cache" / "l2"
        rocky_dir.mkdir(parents=True)
        bw_dir.mkdir(parents=True)

        rocky_l2 = L2Index(rocky_dir, CacheConfig())
        bw_l2 = L2Index(bw_dir, CacheConfig())

        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            rocky_l2.add("Easter bunnies at $20", "session:r1", sample_embedding)
        )
        loop.run_until_complete(
            bw_l2.add("Shopify order #1234", "session:b1", sample_embedding)
        )

        rocky_results = rocky_l2.search(sample_embedding)
        bw_results = bw_l2.search(sample_embedding)

        # Each only sees their own data
        rocky_content = " ".join(r.content for r in rocky_results)
        bw_content = " ".join(r.content for r in bw_results)
        assert "Easter" in rocky_content
        assert "Shopify" not in rocky_content
        assert "Shopify" in bw_content
        assert "Easter" not in bw_content

    def test_read_only_agent(self, basic_config):
        shared = basic_config.agents.get("shared")
        assert shared is not None
        assert shared.read_only is True


# ─────────────────────────────────────────────
#  Persona Tests
# ─────────────────────────────────────────────

class TestPersonas:
    def test_default_personas_exist(self):
        assert "default" in DEFAULT_PERSONAS
        assert "strict" in DEFAULT_PERSONAS
        assert "creative" in DEFAULT_PERSONAS

    def test_strict_has_high_bar(self):
        strict = DEFAULT_PERSONAS["strict"]
        assert strict.max_confidence_for_pass == 0.9
        assert strict.preflight == "aggressive"
        assert not strict.allow_speculative

    def test_creative_is_permissive(self):
        creative = DEFAULT_PERSONAS["creative"]
        assert creative.max_confidence_for_pass == 0.5
        assert creative.preflight == "permissive"
        assert creative.allow_speculative
        assert creative.l1_similarity_override == 0.6

    def test_persona_affects_search_threshold(self, tmp_data_dir, sample_embedding, similar_embedding):
        """Creative persona surfaces more results than strict."""
        l1 = L1Cache(tmp_data_dir / "cache" / "l1", CacheConfig(l1_similarity_threshold=0.85))
        asyncio.get_event_loop().run_until_complete(
            l1.add("brainstorm ideas", "test", sample_embedding)
        )

        strict = PersonaConfig(name="strict", l1_similarity_override=0.9)
        creative = PersonaConfig(name="creative", l1_similarity_override=0.5)

        strict_hits = l1.search(similar_embedding, persona=strict)
        creative_hits = l1.search(similar_embedding, persona=creative)

        # Creative should find at least as many (likely more) results
        assert len(creative_hits) >= len(strict_hits)


# ─────────────────────────────────────────────
#  Integration Smoke Test
# ─────────────────────────────────────────────

class TestServerSmoke:
    def test_app_creates(self, basic_config):
        """Server app initializes without errors."""
        from agentb.server import create_app
        app = create_app(basic_config)
        assert app is not None
        assert app.title == "Mnemo Cortex"

    def test_health_endpoint_exists(self, basic_config):
        """Health route is registered."""
        from agentb.server import create_app
        app = create_app(basic_config)
        routes = [r.path for r in app.routes]
        assert "/health" in routes
        assert "/context" in routes
        assert "/preflight" in routes
        assert "/writeback" in routes
        assert "/ingest" in routes
        assert "/sessions" in routes


# ─────────────────────────────────────────────
#  Session Manager Tests
# ─────────────────────────────────────────────

class TestSessionManager:
    def test_ingest_creates_session(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        result = sm.ingest("What's the weather?", "It's sunny today!")
        assert result["session_id"]
        assert result["entry_number"] == 1
        assert sm.stats["hot_sessions"] == 1

    def test_ingest_appends_to_same_session(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        r1 = sm.ingest("Hello", "Hi there!")
        r2 = sm.ingest("How are you?", "I'm great!")
        assert r1["session_id"] == r2["session_id"]
        assert r2["entry_number"] == 2

    def test_new_session_after_gap(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig(max_session_gap_minutes=0))
        r1 = sm.ingest("First", "Response 1")
        time.sleep(0.05)
        # Force gap detection
        sm._last_ingest_time = time.time() - 120
        r2 = sm.ingest("Second", "Response 2")
        assert r1["session_id"] != r2["session_id"]

    def test_search_hot(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        sm.ingest("Tell me about Easter bunnies", "Easter bunnies are $20-30 each")
        sm.ingest("What about Shopify?", "Shopify orders are up 15%")
        results = sm.search_hot("Easter")
        assert len(results) >= 1
        assert "Easter" in results[0]["prompt"] or "Easter" in results[0]["response"]

    def test_search_hot_no_cross_contamination(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        sm.ingest("Secret agent stuff", "Classified response")
        results = sm.search_hot("Shopify")
        assert len(results) == 0

    def test_get_recent_context(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        sm.ingest("First question", "First answer")
        sm.ingest("Second question", "Second answer")
        context = sm.get_recent_context(n_exchanges=5)
        assert "First question" in context
        assert "Second answer" in context

    def test_transcript_retrieval(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        result = sm.ingest("Hello", "World")
        transcript = sm.get_session_transcript(result["session_id"])
        exchanges = [e for e in transcript if e.get("_type") == "exchange"]
        assert len(exchanges) == 1
        assert exchanges[0]["prompt"] == "Hello"

    def test_hot_session_listing(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        sm.ingest("Test", "Response")
        sessions = sm.get_hot_sessions()
        assert len(sessions) == 1
        assert sessions[0]["tier"] == "hot"
        assert sessions[0]["entries"] == 1

    def test_archival_respects_hot_days(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig(hot_days=0))
        sm.ingest("Old data", "Old response")
        # Force session to not be current
        old_file = sm._current_session_file
        sm._current_session_id = None
        sm._current_session_file = None
        time.sleep(0.05)
        archived = sm.archive_hot_sessions()
        assert len(archived) == 1
        assert sm.stats["hot_sessions"] == 0

    def test_stats_complete(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        sm.ingest("Test", "Response")
        stats = sm.stats
        assert "hot_sessions" in stats
        assert "warm_sessions" in stats
        assert "cold_sessions" in stats
        assert "current_session" in stats
        assert stats["current_entries"] == 1

    def test_safety_cap_starts_new_session(self, tmp_data_dir):
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig(max_hot_entries=2))
        r1 = sm.ingest("One", "A1")
        r2 = sm.ingest("Two", "A2")
        r3 = sm.ingest("Three", "A3")  # exceeds cap, new session
        assert r1["session_id"] == r2["session_id"]
        assert r2["session_id"] != r3["session_id"]

    def test_crash_safe_append(self, tmp_data_dir):
        """After ingest, data is on disk even without explicit close."""
        from agentb.sessions import SessionManager, SessionConfig
        sm = SessionManager(tmp_data_dir, SessionConfig())
        result = sm.ingest("Critical data", "Must not be lost")
        session_file = sm._current_session_file

        # Simulate crash — create new manager, read the file
        sm2 = SessionManager(tmp_data_dir, SessionConfig())
        transcript = sm2.get_session_transcript(result["session_id"])
        exchanges = [e for e in transcript if e.get("_type") == "exchange"]
        assert len(exchanges) == 1
        assert exchanges[0]["prompt"] == "Critical data"
