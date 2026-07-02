"""v4.2 — the Thesaurus Loop (query expansion on a whiff).

Covers the four moving parts:
  - merge_passes        — max-relevance-per-memory_id fusion (the crux); a single
                          pass is byte-identical to the pre-expansion handler.
  - should_expand       — escalation trigger: short query / zero results / FLAT
                          distribution (top - median < gap_threshold) vs a clear
                          winner. Gap signal is embedder-agnostic — the v4.3.0
                          absolute relevance_floor sat inside the noise band and
                          fired 0× (v4.4.0 recalibration).
  - expand_query        — the isolated Flash call: no-key no-op, timeout = [] (no
                          regression), parsing/cap, drop-original, LRU cache.
  - /context end-to-end — escalation fires on a whiff, and never fires for a batch
                          call, an expand=False call, or a strong first pass.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, ExpansionConfig, DEFAULT_PERSONAS,
)
from agentb.cache import ContextChunk
from agentb.server import (
    merge_passes, should_expand, top_relevance, median_relevance, expand_query, _EXPAND_CACHE,
)


def _chunk(mid, rel, tier="VEC", content="c", source=None):
    return ContextChunk(content=content, source=source or f"memory:{mid}",
                        relevance=rel, cache_tier=tier, memory_id=mid)


# ── merge_passes: max-relevance fusion ──────────────────────────────────────

def test_merge_single_pass_is_identity():
    p = [_chunk("a", 0.9), _chunk("b", 0.5), _chunk("c", 0.7)]
    out = merge_passes([p])
    assert [c.memory_id for c in out] == ["a", "b", "c"]
    assert out[0] is p[0]  # same objects, untouched order


def test_merge_keeps_max_relevance_and_original_position():
    p1 = [_chunk("a", 0.30), _chunk("b", 0.60)]
    p2 = [_chunk("a", 0.80), _chunk("d", 0.40)]  # 'a' collides, stronger in p2
    out = merge_passes([p1, p2])
    assert [c.memory_id for c in out] == ["a", "b", "d"]  # 'a' holds first slot
    a = next(c for c in out if c.memory_id == "a")
    assert a.relevance == 0.80  # the better dart wins (NOT first-wins)


def test_merge_lower_relevance_does_not_replace():
    out = merge_passes([[_chunk("a", 0.90)], [_chunk("a", 0.10)]])
    assert len(out) == 1 and out[0].relevance == 0.90


def test_merge_hot_chunks_dedup_by_source_and_content():
    h1 = ContextChunk("same", "hot-session:1", 0.75, "HOT")
    h2 = ContextChunk("same", "hot-session:1", 0.75, "HOT")  # dup of h1
    h3 = ContextChunk("diff", "hot-session:1", 0.75, "HOT")  # distinct content
    out = merge_passes([[h1], [h2, h3]])
    assert len(out) == 2


# ── should_expand / top_relevance: the escalation trigger ───────────────────

def test_top_relevance_empty_is_zero():
    assert top_relevance([]) == 0.0


def test_median_relevance_empty_is_zero():
    assert median_relevance([]) == 0.0


def test_median_relevance_value():
    assert median_relevance([_chunk("a", 0.2), _chunk("b", 0.6), _chunk("c", 0.4)]) == 0.4


def test_short_query_never_expands_even_with_zero_results():
    # 2 words < min_query_words(3): a likely single-entity lookup, skip it.
    assert should_expand("solr index", [], ExpansionConfig()) is False


def test_zero_results_expands():
    assert should_expand("how do we deploy", [], ExpansionConfig()) is True


def test_flat_distribution_expands():
    # The v4.3.0 calibration trap, reproduced: every hit is high-but-uniform,
    # straddling the OLD absolute floor (0.5). Nothing stands out → whiff → expand.
    # top - median = 0.55 - 0.54 = 0.01 < gap_threshold(0.02).
    chunks = [_chunk("a", 0.55), _chunk("b", 0.54), _chunk("c", 0.53), _chunk("d", 0.54)]
    assert should_expand("how do we deploy the service", chunks, ExpansionConfig()) is True


def test_clear_winner_does_not_expand():
    # A real on-topic hit peaks above its own pack regardless of the absolute band.
    # top - median = 0.58 - 0.51 = 0.07 >= gap_threshold(0.02) → strong → skip.
    chunks = [_chunk("a", 0.58), _chunk("b", 0.51), _chunk("c", 0.50), _chunk("d", 0.51)]
    assert should_expand("how do we deploy the service", chunks, ExpansionConfig()) is False


def test_single_uniform_result_expands_accepted_false_positive():
    # One result: top == median → gap 0 < threshold → expands. This is the
    # near-free false-positive the locked design explicitly accepts (one Flash
    # call; max-relevance merge makes the merged result identical to not expanding).
    assert should_expand("how do we deploy the service", [_chunk("a", 0.9)], ExpansionConfig()) is True


def test_gap_threshold_is_configurable():
    # Same pool, two thresholds straddling its 0.04 gap → opposite verdicts.
    chunks = [_chunk("a", 0.58), _chunk("b", 0.54), _chunk("c", 0.54)]  # top-median = 0.04
    assert should_expand("how do we deploy the service", chunks, ExpansionConfig(gap_threshold=0.02)) is False
    assert should_expand("how do we deploy the service", chunks, ExpansionConfig(gap_threshold=0.06)) is True


def test_gap_exactly_at_threshold_does_not_expand():
    # Boundary: top - median == gap_threshold is NOT "< threshold" → no expand.
    chunks = [_chunk("a", 0.53), _chunk("b", 0.50), _chunk("c", 0.50)]  # gap == 0.03
    assert should_expand("how do we deploy the service", chunks, ExpansionConfig(gap_threshold=0.03)) is False


def test_default_retune_flat_on_topic_pool_no_longer_expands():
    # v4.5.3 retune for IGOR-2's nomic band (gap_threshold 0.03 → 0.02): a flat
    # but on-topic pool that rises exactly 0.02 above its pack is NO LONGER a whiff
    # under the default (it was, at 0.03). gap = 0.56 - 0.54 = 0.02; 0.02 < 0.02 is
    # False → skip. A true whiff still peaks only ~0.01 over the pack and expands.
    flat = [_chunk("a", 0.56), _chunk("b", 0.54), _chunk("c", 0.54), _chunk("d", 0.53)]
    assert should_expand("how do we deploy the service", flat, ExpansionConfig()) is False
    whiff = [_chunk("a", 0.55), _chunk("b", 0.54), _chunk("c", 0.54), _chunk("d", 0.54)]
    assert should_expand("how do we deploy the service", whiff, ExpansionConfig()) is True


# ── expand_query: the isolated Flash call ───────────────────────────────────

class _FakeResp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""
    def __init__(self, content=None, exc=None):
        self._content, self._exc = content, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        if self._exc:
            raise self._exc
        return _FakeResp(self._content)


def test_no_api_key_is_a_silent_noop():
    out = asyncio.run(expand_query("a long enough query", ExpansionConfig(), "", "base"))
    assert out == []


def test_happy_path_strips_numbering_and_caps():
    _EXPAND_CACHE.clear()
    cfg = ExpansionConfig(max_variants=2)
    content = "1. first rephrasing here\n- second rephrasing\nthird rephrasing\nfourth one"
    with patch("agentb.server.httpx.AsyncClient", lambda *a, **k: _FakeClient(content=content)):
        out = asyncio.run(expand_query("original query text", cfg, "key", "base"))
    assert out == ["first rephrasing here", "second rephrasing"]  # numbering/bullets stripped, capped to 2


def test_timeout_returns_empty_no_regression():
    _EXPAND_CACHE.clear()
    with patch("agentb.server.httpx.AsyncClient",
               lambda *a, **k: _FakeClient(exc=httpx.TimeoutException("slow"))):
        out = asyncio.run(expand_query("original query text", ExpansionConfig(), "key", "base"))
    assert out == []  # graceful → handler behaves exactly like today


def test_null_content_does_not_crash():
    # A refused / tool-only completion returns content: null. The parse must not
    # AttributeError past the guard (that would 500 the live recall path).
    _EXPAND_CACHE.clear()
    with patch("agentb.server.httpx.AsyncClient", lambda *a, **k: _FakeClient(content=None)):
        out = asyncio.run(expand_query("a long enough query", ExpansionConfig(), "key", "base"))
    assert out == []


def test_digit_leading_phrasing_is_preserved():
    # Char-set lstrip would eat the leading digits ("3D printing" -> "D printing").
    # Only genuine list markers (1. / - / *) should be stripped.
    _EXPAND_CACHE.clear()
    cfg = ExpansionConfig(max_variants=4)
    content = "1. 3D printing workflow\n- 2024 roadmap notes"
    with patch("agentb.server.httpx.AsyncClient", lambda *a, **k: _FakeClient(content=content)):
        out = asyncio.run(expand_query("original query", cfg, "key", "base"))
    assert out == ["3D printing workflow", "2024 roadmap notes"]


def test_original_phrasing_is_dropped():
    _EXPAND_CACHE.clear()
    cfg = ExpansionConfig(max_variants=4)
    content = "original query\na different phrasing"
    with patch("agentb.server.httpx.AsyncClient", lambda *a, **k: _FakeClient(content=content)):
        out = asyncio.run(expand_query("original query", cfg, "key", "base"))
    assert out == ["a different phrasing"]


def test_nonempty_result_is_cached():
    _EXPAND_CACHE.clear()
    cfg = ExpansionConfig(max_variants=3)
    calls = {"n": 0}

    def factory(*a, **k):
        calls["n"] += 1
        return _FakeClient(content="alpha rephrase\nbeta rephrase")

    with patch("agentb.server.httpx.AsyncClient", factory):
        out1 = asyncio.run(expand_query("same query here", cfg, "key", "base"))
        out2 = asyncio.run(expand_query("same query here", cfg, "key", "base"))
    assert out1 == out2 == ["alpha rephrase", "beta rephrase"]
    assert calls["n"] == 1  # second call served from the LRU


def test_failure_is_not_cached():
    _EXPAND_CACHE.clear()
    cfg = ExpansionConfig()
    with patch("agentb.server.httpx.AsyncClient",
               lambda *a, **k: _FakeClient(exc=httpx.ConnectError("down"))):
        asyncio.run(expand_query("a long enough query", cfg, "key", "base"))
    assert len(_EXPAND_CACHE) == 0  # transient failure must not poison the cache


# ── /context end-to-end: escalation wiring ──────────────────────────────────

_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}
VEC = [0.0] * 768
VEC[0] = 1.0


class _RecordingEmbedding:
    active_label = "fake/embed"

    def __init__(self):
        self.embedded: list[str] = []

    @property
    def status(self):
        return _STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        self.embedded.append(text)
        return list(VEC)

    async def health_check(self):
        return True


class _DiscriminatingEmbedding(_RecordingEmbedding):
    """Unlike the uniform _RecordingEmbedding, embeds 'deploy'-mentioning text to
    e0 = [1,0,...] and everything else off-axis ([1,0.5,...]), so a real on-topic
    memory lands at cosine 1.0 and fillers at 1/sqrt(1.25)=0.894 through the ACTUAL
    L3 cosine path. Proves the gap signal works end-to-end on real cosine spread,
    not just hand-fed relevances (gap = 1.0 - 0.894 = 0.106 >= gap_threshold)."""
    active_label = "fake/embed-discriminating"

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        self.embedded.append(text)
        v = [0.0] * 768
        v[0] = 1.0
        if "deploy" not in text.lower():
            v[1] = 0.5
        return v


class _FakeReasoning:
    active_label = "fake/reason"

    @property
    def status(self):
        return _STATUS

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return "topology"

    async def health_check(self):
        return True


def _make_client(tmp_path, embedder):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic")),
        cache=CacheConfig(), server=ServerConfig(port=50097),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        expansion=ExpansionConfig(),  # default-ON
        personas=dict(DEFAULT_PERSONAS),
    )
    patch_embed = patch("agentb.server.create_resilient_embedding", return_value=embedder)
    patch_reason = patch("agentb.server.create_resilient_reasoning", return_value=_FakeReasoning())
    patch_embed.start()
    patch_reason.start()
    from agentb.server import create_app
    client = TestClient(create_app(cfg))
    client.__enter__()
    return client, (patch_embed, patch_reason)


def _seed_memory(tmp_path, memory_id, summary, category="doctrine"):
    """Seed one memory into VEC's store + its disk JSON so a normal recall finds
    it at high relevance (FakeEmbedding maps everything to the same vector)."""
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": summary, "key_facts": [],
        "category": category, "source": "tool", "created_at": now,
    }))


def test_escalation_fires_on_empty_store():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        tmp = Path(td)
        emb = _RecordingEmbedding()
        client, patches = _make_client(tmp, emb)
        try:
            mock_expand = AsyncMock(return_value=["an alternate phrasing of it"])
            with patch("agentb.server.expand_query", mock_expand):
                r = client.post("/context", json={"prompt": "how do we deploy this", "max_results": 5})
            assert r.status_code == 200, r.text
            mock_expand.assert_awaited_once()                 # whiff → escalated
            assert "an alternate phrasing of it" in emb.embedded  # variant got searched
        finally:
            client.__exit__(None, None, None)
            for p in patches:
                p.stop()


def test_batch_call_never_expands():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        client, patches = _make_client(tmp, _RecordingEmbedding())
        try:
            mock_expand = AsyncMock(return_value=["x"])
            with patch("agentb.server.expand_query", mock_expand):
                r = client.post("/context", json={"prompt": "how do we deploy this",
                                                  "max_results": 5, "batch": True})
            assert r.status_code == 200, r.text
            mock_expand.assert_not_awaited()  # batch is live-path-exempt
        finally:
            client.__exit__(None, None, None)
            for p in patches:
                p.stop()


def test_expand_false_never_expands():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        client, patches = _make_client(tmp, _RecordingEmbedding())
        try:
            mock_expand = AsyncMock(return_value=["x"])
            with patch("agentb.server.expand_query", mock_expand):
                r = client.post("/context", json={"prompt": "how do we deploy this",
                                                  "max_results": 5, "expand": False})
            assert r.status_code == 200, r.text
            mock_expand.assert_not_awaited()
        finally:
            client.__exit__(None, None, None)
            for p in patches:
                p.stop()


def test_clear_winner_first_pass_skips_expansion():
    # A real on-topic memory peaks above filler memories (cosine 1.0 vs 0.894)
    # through the actual L3 cosine path, so top - median = 0.106 >= gap_threshold
    # → clear winner → no whiff → no expansion.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_memory(tmp, "m-strong", "deployment runbook for the service")
        for i in range(3):
            _seed_memory(tmp, f"m-filler-{i}", f"an unrelated note number {i}")
        client, patches = _make_client(tmp, _DiscriminatingEmbedding())
        try:
            mock_expand = AsyncMock(return_value=["x"])
            with patch("agentb.server.expand_query", mock_expand):
                r = client.post("/context", json={"prompt": "how do we deploy this", "max_results": 5})
            assert r.status_code == 200, r.text
            assert r.json()["total_found"] >= 1
            mock_expand.assert_not_awaited()
        finally:
            client.__exit__(None, None, None)
            for p in patches:
                p.stop()


def test_flat_high_distribution_expands():
    # THE v4.3.0 regression: multiple hits, all uniformly HIGH (relevance 1.0),
    # but no standout. The old absolute floor (0.5) saw 1.0 >= 0.5 and never
    # expanded; the gap signal sees top == median (gap 0) and correctly escalates.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i in range(4):
            _seed_memory(tmp, f"m-{i}", f"some memory content {i}")
        client, patches = _make_client(tmp, _RecordingEmbedding())
        try:
            mock_expand = AsyncMock(return_value=["an alternate phrasing here"])
            with patch("agentb.server.expand_query", mock_expand):
                r = client.post("/context", json={"prompt": "how do we deploy this", "max_results": 5})
            assert r.status_code == 200, r.text
            mock_expand.assert_awaited_once()  # flat pool → whiff → escalate
        finally:
            client.__exit__(None, None, None)
            for p in patches:
                p.stop()
