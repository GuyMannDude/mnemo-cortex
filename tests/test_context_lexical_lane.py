"""The lexical lane on the wire (v4.21): the server-side block in /context.

test_vec.py pins lexical_terms/lexical_search and test_ranking.py the
composite term; the harnesses pin the outcome. This file pins the join
itself — a memory OUTSIDE the kNN's returned set reaches the caller as a
LEX chunk — and the two failure paths the block must degrade through.
"""
from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig, RankingConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.vec import EMBED_DIM, VecStore

_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


def _axis(i: int, sign: float = 1.0) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[i] = sign
    return v


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"): return _axis(0)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "decision"
    async def health_check(self): return True


def _client(tmp_path, ranking=None):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50098),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        ranking=ranking or RankingConfig(), personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        return TestClient(create_app(cfg))


def _seed(tmp_path, memory_id, summary, vec, age_days=1.0):
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    ts = time.time() - age_days * 86400
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": summary, "key_facts": [],
        "category": "decision", "source": "user", "created_at": ts,
    }))
    store = VecStore(base / "vec_index.sqlite")
    store.upsert(memory_id, summary, vec, source_file=(mem_dir / f"{memory_id}.json").as_posix(),
                 created_at=ts, category="decision")
    store.close()


def _seed_world(tmp_path):
    """Query = axis 0. `near` sits ON the query; seven orthogonal fillers;
    `target` is the antipode (cosine -1), guaranteed outside a kNN of 7 —
    which is the overfetch for max_results=2 (max(2*3, 2+5) = 7)."""
    _seed(tmp_path, "near", "the memory server listens on the workstation", _axis(0))
    for i in range(1, 8):
        _seed(tmp_path, f"filler-{i}", f"filler memory number {i} about nothing in particular", _axis(i))
    _seed(tmp_path, "target", "advisory GHSA-7q2x-4mvp-9rrk accepted: branch unreachable", _axis(0, -1.0))


def _context(client, prompt, **kw):
    r = client.post("/context", json={"prompt": prompt, "max_results": 2, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_memory_outside_the_knn_reaches_the_caller_as_a_lex_chunk(tmp_path):
    _seed_world(tmp_path)
    with _client(tmp_path) as client:
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    served = {c["memory_id"]: c for c in body["chunks"]}
    assert "target" in served, body
    assert served["target"]["cache_tier"] == "LEX"
    assert served["near"]["cache_tier"] == "VEC"
    # 4.21.1: the prompt named a rare identifier; the memory holding it is
    # served FIRST even at cosine -1 — the prompt picked it, meaning orders the rest
    assert [c["memory_id"] for c in body["chunks"]] == ["target", "near"]
    assert body["cache_hits"]["LEX"] == 1 and body["cache_hits"]["VEC"] == 1
    # the LEX chunk's relevance is on VEC's wire scale: cosine -1 → d = 2 → 1/3
    assert served["target"]["relevance"] == pytest.approx(1 / 3, abs=1e-3)


def test_a_word_match_without_a_rare_identifier_does_not_pin(tmp_path):
    """Same world, prompt shares topical words with `target` but names no
    identifier: the lane attaches evidence, meaning still wins slot 1."""
    _seed_world(tmp_path)
    with _client(tmp_path) as client:
        body = _context(client, "which advisory did we accept as unreachable")
    assert body["chunks"][0]["memory_id"] == "near"


def test_an_identifier_many_memories_share_is_a_topic_not_a_pin(tmp_path):
    """'2026' or a port everyone mentions must not pin: past LEX_EXACT_MAX_DF
    memories the term is a topic, and the composite decides as before."""
    from agentb.vec import LEX_EXACT_MAX_DF
    _seed_world(tmp_path)
    for i in range(LEX_EXACT_MAX_DF + 1):
        _seed(tmp_path, f"shared-{i}", f"note {i} mentions build 8010 too", _axis(9 + i))
    with _client(tmp_path) as client:
        body = _context(client, "what about build 8010")
    assert body["chunks"][0]["memory_id"] == "near"       # cosine 1.0 still first
    assert not any(c["memory_id"].startswith("shared-") and i == 0
                   for i, c in enumerate(body["chunks"]))


def test_pins_never_own_the_window(tmp_path):
    """Five hashes pasted beside a real question: the pins take at most
    half the window, the question's answer (cosine 1.0) is still served."""
    _seed_world(tmp_path)
    hashes = ["a1b2c3d", "b2c3d4e", "c3d4e5f", "d4e5f6a", "e5f6a7b"]
    for i, h in enumerate(hashes):
        _seed(tmp_path, f"log-{h}", f"session log: commit {h} landed", _axis(0, -1.0))
    with _client(tmp_path) as client:
        r = client.post("/context", json={
            "prompt": "in light of commits " + " ".join(hashes) + ", what does the memory server listen on",
            "max_results": 4})
    assert r.status_code == 200, r.text
    served = [c["memory_id"] for c in r.json()["chunks"]]
    # exactly two pins lead (half of 4), then the answer; a third pinned log
    # may follow on its own score — the fillers are equally irrelevant here
    assert all(m.startswith("log-") for m in served[:2]), served
    assert served[2] == "near", served


def test_a_dotted_version_pins_and_a_two_part_number_does_not(tmp_path):
    """4.21.1's stated limit: '4.20.2' tokenised into filler and the memory
    holding it sat at lexical rank 19. 4.21.3: the run is a phrase term in
    both spellings, and three parts is identifier-shaped."""
    _seed_world(tmp_path)
    _seed(tmp_path, "release", "mnemo v4.20.2 shipped the seal fix", _axis(0, -1.0))
    _seed(tmp_path, "runtime", "python 3.12 is the interpreter on the laptop", _axis(0, -1.0))
    with _client(tmp_path) as client:
        version = _context(client, "what changed in 4.20.2")
        two_part = _context(client, "which python 3.12 runs on the laptop")
    # the store spells it 'v4.20.2'; the prompt's bare spelling still pins it
    assert version["chunks"][0]["memory_id"] == "release", version
    assert version["chunks"][0]["cache_tier"] == "LEX"
    # two parts is a topic: cosine 1.0 keeps slot 1
    assert two_part["chunks"][0]["memory_id"] == "near", two_part


def test_pins_order_newest_first_through_the_served_window(tmp_path):
    """Two memories hold the hash. The OLDER one sits on the query (cosine
    1.0, the composite's favourite); the newer one is at the antipode.
    Pins order by date, not score: newer first — and the order is the
    same on a second call, after the first call's access bump."""
    _seed_world(tmp_path)
    _seed(tmp_path, "older", "commit 4b1d9e2 planned for the memory server", _axis(0), age_days=30.0)
    _seed(tmp_path, "newer", "commit 4b1d9e2 landed and sealed", _axis(0, -1.0), age_days=2.0)
    # a window of 4 lets two pins lead (the cap is half the window)
    with _client(tmp_path) as client:
        first = _context(client, "what happened with commit 4b1d9e2", max_results=4)
        second = _context(client, "what happened with commit 4b1d9e2", max_results=4)
    assert [c["memory_id"] for c in first["chunks"]][:2] == ["newer", "older"], first
    assert [c["memory_id"] for c in second["chunks"]][:2] == ["newer", "older"], second


def test_a_bare_four_digit_number_does_not_pin(tmp_path):
    _seed_world(tmp_path)
    _seed(tmp_path, "offtopic", "the printer jam cost us 2000 sheets", _axis(0, -1.0))
    with _client(tmp_path) as client:
        focus = _context(client, "write me the 2000 word session summary")
        recent = _context(client, "write me the 2000 word session summary", mode="recent")
    # focus: no pin means meaning keeps slot 1 (the word match only earns
    # the ordinary bonus, which in this all-irrelevant filler world is slot 2)
    assert focus["chunks"][0]["memory_id"] == "near"
    # recent: not pinned, so out of band stays out — no band-gate bypass
    assert "offtopic" not in {c["memory_id"] for c in recent["chunks"]}


def test_recent_lens_pins_too_and_explore_does_not(tmp_path):
    _seed_world(tmp_path)
    # a NEWER on-topic memory: the date sort alone would put it first, so
    # `target` leading proves the pin, not the sort
    base = tmp_path / "agents" / "default"
    newer = base / "memory" / "newer.json"
    newer.write_text(json.dumps({"id": "newer", "summary": "the memory server moved hosts today",
                                 "key_facts": [], "category": "decision", "source": "user",
                                 "created_at": time.time()}))
    st = VecStore(base / "vec_index.sqlite")
    st.upsert("newer", "the memory server moved hosts today", _axis(0), source_file=newer.as_posix(),
              created_at=time.time(), category="decision")
    st.close()
    with _client(tmp_path) as client:
        recent = _context(client, "GHSA-7q2x-4mvp-9rrk", mode="recent")
        explore = _context(client, "GHSA-7q2x-4mvp-9rrk", mode="explore")
    # recent: in-band chunks by date, then the pin goes first. `target` is at
    # cosine -1 — out of band — so it can only appear through the pin.
    assert [c["memory_id"] for c in recent["chunks"]] == ["target", "newer"]
    # explore keeps its serendipity: out-of-band chunks stay out, no pin
    assert "target" not in {c["memory_id"] for c in explore["chunks"]}


def test_lane_off_is_pure_vector_recall(tmp_path):
    _seed_world(tmp_path)
    with _client(tmp_path, RankingConfig(lexical_enabled=False)) as client:
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert "target" not in {c["memory_id"] for c in body["chunks"]}
    assert body["cache_hits"]["LEX"] == 0
    assert all(c["cache_tier"] == "VEC" for c in body["chunks"])


def test_lexical_row_without_a_vector_is_skipped(tmp_path):
    """A vec_lex row whose vector is gone (a torn upsert, a hand edit) is
    not a served memory: the lane must skip it, not raise or fabricate."""
    _seed_world(tmp_path)
    store = VecStore(tmp_path / "agents" / "default" / "vec_index.sqlite")  # vec0 needs the extension loaded
    with store._conn:
        store._conn.execute("DELETE FROM vec_embeddings WHERE memory_id = 'target'")
    store.close()
    with _client(tmp_path) as client:
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert "target" not in {c["memory_id"] for c in body["chunks"]}
    assert body["cache_hits"]["LEX"] == 0


def test_a_broken_lexical_table_degrades_to_vector_recall_out_loud(tmp_path, caplog):
    _seed_world(tmp_path)
    with _client(tmp_path) as client, \
         patch.object(VecStore, "lexical_search", side_effect=sqlite3.OperationalError("no such module: fts5")):
        body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert body["chunks"] and all(c["cache_tier"] == "VEC" for c in body["chunks"])
    assert "lexical lane failed" in caplog.text


def test_dimension_mismatch_keeps_the_lane_out_of_the_pool(tmp_path):
    """The kNN screams and serves nothing on a query of the wrong dimension
    so L3 can rescue the recall; the lane must not fill the pool with
    cosines over a truncating zip."""
    _seed_world(tmp_path)

    class ShortEmbedding(FakeEmbedding):
        async def embed(self, text, *, use_breaker=True, task_type="document"):
            return [1.0] * (EMBED_DIM // 2)

    with patch("agentb.server.create_resilient_embedding", return_value=ShortEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        cfg = AgentBConfig(
            reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
            embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
            cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50098),
            data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
            personas=dict(DEFAULT_PERSONAS),
        )
        with TestClient(create_app(cfg)) as client:
            body = _context(client, "what did we decide on GHSA-7q2x-4mvp-9rrk")
    assert body["cache_hits"]["LEX"] == 0
    assert all(c["cache_tier"] != "LEX" for c in body["chunks"])
