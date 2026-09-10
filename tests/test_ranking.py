"""v4.1 composite recall ranking — scoring unit tests + /context behavior.

The contract under test: similarity still dominates (an irrelevant memory
can never out-rank a strong match), but within the band of plausible matches
a doctrine beats a session log, fresh beats ancient, and frequently-recalled
beats never-recalled. Chunks with missing metadata get neutral values, never
penalties (pre-v3 records must stay accessible).
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.ranking import composite_score, pool_similarities, SIMILARITY_SPAN, _to_cosine, order_revisions, exact_first
from agentb.vec import VecStore
from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig, RankingConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)

CFG = RankingConfig()


def _score(sim=0.7, age=None, cat=None, access=0):
    return composite_score(similarity=sim, age_days=age, category=cat,
                           access_count=access, cfg=CFG)


# ── Unit: score ordering ──

def test_similarity_dominates():
    strong_log = _score(sim=0.9, cat="session_log")
    weak_doctrine = _score(sim=0.2, cat="doctrine")
    assert strong_log > weak_doctrine


def test_category_breaks_ties():
    assert _score(cat="doctrine") > _score(cat="topology") > _score(cat="session_log")


def test_recency_breaks_ties():
    assert _score(age=1) > _score(age=90)


def test_access_breaks_ties_and_saturates():
    assert _score(access=5) > _score(access=0)
    # saturating: 100 recalls worth barely more than 10
    assert _score(access=100) - _score(access=10) < 0.02


def test_missing_metadata_is_neutral_not_penalized():
    # unknown age must land between fresh and ancient, not below both
    assert _score(age=200) < _score(age=None) < _score(age=2)
    # uncategorized must beat session_log (it might be gold; a log is known noise)
    assert _score(cat=None) > _score(cat="session_log")


def test_weights_come_from_config():
    flat = RankingConfig(w_similarity=1.0, w_recency=0.0, w_importance=0.0, w_access=0.0)
    a = composite_score(similarity=0.5, age_days=1, category="doctrine",
                        access_count=50, cfg=flat)
    b = composite_score(similarity=0.5, age_days=900, category="session_log",
                        access_count=0, cfg=flat)
    assert a == pytest.approx(b)


# ── Unit: pool similarity normalisation (Experiment One) ──

def _rel(cos):
    """What the VEC tier reports for a stored unit vector at this cosine:
    L2 = sqrt(2 - 2cos), relevance = 1/(1+L2)."""
    import math
    return 1.0 / (1.0 + math.sqrt(max(0.0, 2.0 - 2.0 * cos)))


def test_vec_relevance_maps_back_to_cosine():
    # pins the actual formula, not just monotonicity: a stored vector at
    # cosine 0.9 from the query reports rel 1/(1+0.447) = 0.691 and must come
    # back as 0.9; a production-band rel of 0.553 is cosine ~0.673.
    assert _to_cosine(_rel(0.9), "VEC") == pytest.approx(0.9)
    assert _to_cosine(0.553, "VEC") == pytest.approx(0.673, abs=0.002)
    assert _to_cosine(0.9, "L1") == 0.9          # already cosine — untouched
    assert _to_cosine(0.0, "VEC") == 0.0          # zero relevance is safe


def test_pool_best_hit_is_one_and_band_becomes_dominant():
    # a served on-topic pool from the live probe spans rel 0.527..0.583
    # (cosine ~0.60..0.74, range ~0.147). Raw, the 0.55-weighted term spanned
    # ~0.03 across this band and lost to recency (max spread 0.20); anchored
    # on the top it must beat recency's maximum.
    rels = [0.583, 0.570, 0.553, 0.540, 0.527]
    sims = pool_similarities([(r, "VEC") for r in rels])
    assert sims[0] == 1.0
    assert sims == sorted(sims, reverse=True)      # order-preserving
    assert sims[-1] == pytest.approx(1.0 - 0.147 / SIMILARITY_SPAN, abs=0.01)
    assert CFG.w_similarity * (sims[0] - sims[-1]) > CFG.w_recency


def test_pool_hair_width_gap_stays_small():
    # two near-identical matches (cosine 0.98 vs 0.955): min-max would hand
    # the closer one the full 0.55 term. Anchored, the gap is 0.025 / SPAN, so
    # category can still re-order — the standing tie-band contract.
    sims = pool_similarities([(_rel(0.98), "VEC"), (_rel(0.955), "VEC")])
    assert sims[0] == 1.0
    assert sims[0] - sims[1] == pytest.approx(0.025 / SIMILARITY_SPAN, abs=1e-3)
    noise = composite_score(similarity=sims[0], age_days=None, category="unknown",
                            access_count=0, cfg=CFG)
    doctrine = composite_score(similarity=sims[1], age_days=None, category="doctrine",
                               access_count=0, cfg=CFG)
    assert doctrine > noise


def test_pool_old_exact_doctrine_beats_fresh_marginal_state():
    # the hazard the clean-room review named: a 90-day doctrine at the top of
    # the band lost to a 2-day current_state at the bottom of it.
    sims = pool_similarities([(0.583, "VEC"), (0.527, "VEC")])
    old_doctrine = composite_score(similarity=sims[0], age_days=90, category="doctrine",
                                   access_count=0, cfg=CFG)
    fresh_state = composite_score(similarity=sims[1], age_days=2, category="current_state",
                                  access_count=5, cfg=CFG)
    assert old_doctrine > fresh_state
    # and RAW relevance (the previous input) reproduces the bug
    assert composite_score(similarity=0.583, age_days=90, category="doctrine",
                           access_count=0, cfg=CFG) < \
           composite_score(similarity=0.527, age_days=2, category="current_state",
                           access_count=5, cfg=CFG)


def test_pool_top_band_ignores_the_off_topic_tail():
    # /context ranks the full overfetch pool (30+ candidates). The on-topic
    # head must score the same whether or not a long off-topic tail is
    # present — min-max over the pool would have re-compressed it.
    head = [(0.583, "VEC"), (0.560, "VEC"), (0.540, "VEC")]
    tail = [(0.50 - i * 0.004, "VEC") for i in range(27)]   # rel 0.50→0.396 = cosine 0.50 down to −0.16
    alone = pool_similarities(head)
    with_tail = pool_similarities(head + tail)
    assert with_tail[:3] == pytest.approx(alone)
    assert all(s == 0.0 for s in with_tail[-10:])            # far tail is zeroed, not negative


def test_pool_edge_cases():
    assert pool_similarities([]) == []
    assert pool_similarities([(0.55, "VEC")]) == [1.0]                 # lone hit is the best hit
    assert pool_similarities([(0.55, "VEC"), (0.55, "VEC")]) == [1.0, 1.0]  # flat pool: all best
    hot_and_dead = pool_similarities([(0.0, "VEC"), (0.75, "HOT")])
    assert hot_and_dead == [0.0, 1.0]                                   # sentinel anchors, dead hit floors


# ── /context integration: doctrine out-ranks noise at lower similarity ──

VEC_A = [0.0] * 768
VEC_A[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"): return list(VEC_A)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "decision"
    async def health_check(self): return True


@pytest.fixture
def client(tmp_path):
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
        with TestClient(create_app(cfg)) as c:
            yield c


def _seed_vec_memory(tmp_path, memory_id, summary, category, *, distance_vec, created_at=None):
    """Write a memory JSON + vec row directly. distance_vec controls how far the
    stored embedding sits from the query vector (VEC_A), i.e. raw similarity."""
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    ts = created_at or time.time()
    (mem_dir / f"{memory_id}.json").write_text(json.dumps({
        "id": memory_id, "summary": summary, "key_facts": [],
        "category": category, "source": "user", "created_at": ts,
    }))
    store = VecStore(base / "vec_index.sqlite")
    store.upsert(memory_id, summary, distance_vec,
                 source_file=(mem_dir / f"{memory_id}.json").as_posix(), created_at=ts)
    store.close()


def test_context_ranks_doctrine_above_unknown_noise(tmp_path, client):
    # noise: slightly CLOSER to the query (higher raw similarity), category
    # unknown. The gap is the realistic tie-band from the quality audit —
    # composite ranking re-orders within that band; a LARGE similarity gap
    # would (correctly) still let the closer match win.
    near = list(VEC_A); near[1] = 0.2
    far = list(VEC_A); far[1] = 0.3
    _seed_vec_memory(tmp_path, "noise1", "uncategorized migration blob", "unknown",
                     distance_vec=near)
    _seed_vec_memory(tmp_path, "gold1", "DOCTRINE: brain files win over Mnemo on conflict",
                     "doctrine", distance_vec=far)

    r = client.post("/context", json={"prompt": "truth hierarchy", "max_results": 2})
    assert r.status_code == 200, r.text
    chunks = r.json()["chunks"]
    assert [c["memory_id"] for c in chunks][0] == "gold1", (
        "doctrine should out-rank unknown noise despite lower raw similarity")


def test_context_access_counts_persist_and_rise(tmp_path, client):
    _seed_vec_memory(tmp_path, "m1", "a decision about ports", "decision",
                     distance_vec=list(VEC_A))
    client.post("/context", json={"prompt": "ports", "max_results": 1})
    client.post("/context", json={"prompt": "ports", "max_results": 1})
    store = VecStore(tmp_path / "agents" / "default" / "vec_index.sqlite")
    counts = store.access_counts(["m1"])
    store.close()
    assert counts.get("m1", 0) >= 2


def test_context_exposes_memory_id(tmp_path, client):
    _seed_vec_memory(tmp_path, "mid1", "identity: Rocky is Hermes on IGOR", "identity",
                     distance_vec=list(VEC_A))
    r = client.post("/context", json={"prompt": "who is rocky", "max_results": 1})
    assert r.json()["chunks"][0]["memory_id"] == "mid1"


# ── Unit: revision order inside the served window (E1, proving ground S02) ──

class _Chunk:
    def __init__(self, memory_id, category="topology", revises=None):
        self.memory_id, self.category, self.revises = memory_id, category, revises or []


def _ids(chunks):
    return [c.memory_id for c in chunks]


def test_reviser_moves_ahead_of_what_it_revised_and_nothing_leaves():
    # the S02 shape: the stale memory echoes the query and scored first; the
    # forced-through revision was second. The revision goes first; the stale
    # one is still served, right behind it.
    stale, fresh, other = _Chunk("stale"), _Chunk("fresh", revises=["stale"]), _Chunk("other")
    assert _ids(order_revisions([stale, fresh, other])) == ["fresh", "stale", "other"]


def test_a_revision_of_a_memory_outside_the_window_changes_nothing():
    # review 2026-09-05: an unbounded demotion ejected a 0.90 top hit on the
    # say-so of a 0.10 chunk. The window is what the caller sees; only its
    # members order each other, so a link to a chunk that is not served is inert.
    assert _ids(order_revisions([_Chunk("a"), _Chunk("b", revises=["gone"])])) == ["a", "b"]


def test_order_revisions_converges_on_a_graph_that_needs_more_passes_than_elements():
    # review 2026-09-05: n+1 passes left this 6-element graph unsettled (needs 8)
    m = {k: _Chunk(k) for k in ["m0", "m1", "m2", "m3", "m4", "m5"]}
    m["m5"].revises, m["m4"].revises, m["m1"].revises = ["m3"], ["m3"], ["m0"]
    m["m3"].revises, m["m2"].revises = ["m0", "m2"], ["m1"]
    out = _ids(order_revisions([m[k] for k in ["m0", "m5", "m4", "m1", "m3", "m2"]]))
    for c in m.values():
        for old in c.revises:
            assert out.index(c.memory_id) < out.index(old), (c.memory_id, old, out)


def test_session_log_reviser_never_moves():
    doc, log = _Chunk("doc", category="doctrine"), _Chunk("log", category="session_log", revises=["doc"])
    assert _ids(order_revisions([doc, log])) == ["doc", "log"]


def test_fan_in_puts_both_revisers_first_and_keeps_the_revised():
    j, i1, i2 = _Chunk("J"), _Chunk("i1", revises=["J"]), _Chunk("i2", revises=["J"])
    assert _ids(order_revisions([j, i1, i2])) == ["i1", "i2", "J"]


def test_revision_chain_settles():
    a, b, c = _Chunk("a"), _Chunk("b", revises=["a"]), _Chunk("c", revises=["b"])
    assert _ids(order_revisions([a, b, c])) == ["c", "b", "a"]
    assert _ids(order_revisions([c, b, a])) == ["c", "b", "a"]        # already ordered: untouched


# ── Integration: the recent lens (E1, proving ground S03) ──

def test_recent_lens_orders_on_topic_by_date_and_drops_the_noise_band(tmp_path, client):
    # three near-identical session memories: the NEWEST is the worst-worded
    # match (cosine 0.94 vs 0.99 at the top — the S03 shape, where focus let
    # the wording gap beat the age gap), plus an off-band noise memory.
    import math
    now = time.time()

    def _at(cos):
        v = [0.0] * 768
        v[0], v[1] = cos, math.sqrt(1.0 - cos * cos)
        return v

    _seed_vec_memory(tmp_path, "old", "session 1: working on the ranker", "session_log",
                     distance_vec=_at(0.99), created_at=now - 13 * 86400)
    _seed_vec_memory(tmp_path, "mid", "session 2: working on the facts ladder", "session_log",
                     distance_vec=_at(0.97), created_at=now - 6 * 86400)
    _seed_vec_memory(tmp_path, "new", "session 3: working on the eject holder", "session_log",
                     distance_vec=_at(0.94), created_at=now)
    _seed_vec_memory(tmp_path, "noise", "lunch was a sandwich", "session_log",
                     distance_vec=_at(0.60), created_at=now - 1 * 86400)

    body = {"prompt": "what was I working on", "max_results": 5, "exclude_categories": []}
    focus = client.post("/context", json={**body, "mode": "focus"}).json()
    assert [c["memory_id"] for c in focus["chunks"]][0] == "old"   # the failure the lens exists for

    r = client.post("/context", json={**body, "mode": "recent"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "recent"
    assert [c["memory_id"] for c in data["chunks"]] == ["new", "mid", "old"]   # noise is out of band


def test_recent_lens_is_a_valid_mode_and_a_typo_is_not(client):
    assert client.post("/context", json={"prompt": "x", "mode": "recnt"}).status_code == 422


def test_recent_lens_shows_session_logs_without_an_exclude_list(tmp_path, client):
    # review 2026-09-05: the bridge forwards exclude_categories only when the
    # caller passes one; with DEFAULT_HIDDEN applied the lens returned
    # everything except the session memories it exists for.
    _seed_vec_memory(tmp_path, "log1", "session: working on the ranker", "session_log",
                     distance_vec=list(VEC_A))
    focus = client.post("/context", json={"prompt": "what was I doing", "mode": "focus"}).json()
    assert focus["chunks"] == []                                     # hidden by default on focus
    recent = client.post("/context", json={"prompt": "what was I doing", "mode": "recent"}).json()
    assert [c["memory_id"] for c in recent["chunks"]] == ["log1"]     # served on recent
    hidden = client.post("/context", json={"prompt": "what was I doing", "mode": "recent",
                                           "exclude_categories": ["session_log"]}).json()
    assert hidden["chunks"] == []                                    # the caller's word still wins


def test_recent_lens_retrieves_the_newest_row_outside_the_knn_pool(tmp_path, client):
    # 20 memories; the NEWEST is the 20th-nearest (cosine 0.80 vs 0.90+ for
    # the rest) so a 15-wide kNN pool never holds it — yet it is in band
    # (0.20 cosine below the best) and must be served first by recent.
    import math
    now = time.time()

    def _at(cos):
        v = [0.0] * 768
        v[0], v[1] = cos, math.sqrt(1.0 - cos * cos)
        return v

    for i in range(19):
        _seed_vec_memory(tmp_path, f"s{i:02d}", f"session {i}: open work", "session_log",
                         distance_vec=_at(0.99 - i * 0.004), created_at=now - (20 - i) * 86400)
    _seed_vec_memory(tmp_path, "newest", "session 19: open work", "session_log",
                     distance_vec=_at(0.80), created_at=now)
    body = {"prompt": "what was I working on", "max_results": 5}
    focus = client.post("/context", json={**body, "mode": "focus", "exclude_categories": []}).json()
    assert "newest" not in [c["memory_id"] for c in focus["chunks"]]
    recent = client.post("/context", json={**body, "mode": "recent"}).json()
    assert [c["memory_id"] for c in recent["chunks"]][0] == "newest"


def test_recent_lens_survives_a_dim_mismatch_without_fabricating_hits(tmp_path, client):
    # review 2026-09-05: newest() truncated the zip on a short query and fed
    # made-up distances into the pool, which also marked VEC as served and
    # skipped the L3 escape hatch. Same guard as search(), same scream.
    from agentb.vec import VecDimMismatch, VecStore
    _seed_vec_memory(tmp_path, "log1", "session: working", "session_log", distance_vec=list(VEC_A))
    store = VecStore(tmp_path / "agents" / "default" / "vec_index.sqlite")
    with pytest.raises(VecDimMismatch):
        store.newest([1.0, 0.0], n=3)
    store.close()


# ── v4.21: the lexical lane's term ──────────────────────────────────────────

def test_lex_tier_maps_to_cosine_like_vec():
    # LEX chunks carry VEC's wire scale so merge_passes compares the lanes directly
    assert _to_cosine(_rel(0.9), "LEX") == pytest.approx(_to_cosine(_rel(0.9), "VEC"))


def test_lexical_evidence_reorders_the_band_but_never_outranks_meaning():
    on_topic = composite_score(similarity=1.0, age_days=10, category="topology",
                               access_count=0, cfg=CFG, lexical=0.0)
    on_topic_with_words = composite_score(similarity=1.0, age_days=10, category="topology",
                                          access_count=0, cfg=CFG, lexical=1.0)
    off_topic_with_words = composite_score(similarity=0.0, age_days=10, category="topology",
                                           access_count=0, cfg=CFG, lexical=1.0)
    assert on_topic_with_words > on_topic                 # the term counts
    assert on_topic_with_words - on_topic == pytest.approx(CFG.w_lexical)
    assert off_topic_with_words < on_topic                # words alone cannot win
    assert composite_score(similarity=0.5, age_days=None, category=None, access_count=0,
                           cfg=CFG) == composite_score(similarity=0.5, age_days=None, category=None,
                                                       access_count=0, cfg=CFG, lexical=0.0)


def test_lexical_term_clamps_and_reads_its_weight_from_config():
    cfg = RankingConfig(w_lexical=0.4)
    base = composite_score(similarity=0.5, age_days=None, category=None, access_count=0, cfg=cfg)
    assert composite_score(similarity=0.5, age_days=None, category=None, access_count=0,
                           cfg=cfg, lexical=5.0) - base == pytest.approx(0.4)
    assert composite_score(similarity=0.5, age_days=None, category=None, access_count=0,
                           cfg=cfg, lexical=-1.0) == pytest.approx(base)


def test_exact_first_is_a_stable_partition():
    class C:
        def __init__(self, name, exact=False): self.name, self.exact = name, exact
    a, b, c, d = C("a"), C("b", True), C("c"), C("d", True)
    assert [x.name for x in exact_first([a, b, c, d])] == ["b", "d", "a", "c"]
    assert [x.name for x in exact_first([a, c])] == ["a", "c"]
    assert exact_first([]) == []
    assert [x.name for x in exact_first([C("z")])] == ["z"]   # no attribute surprises on plain chunks
    # the cap: only `limit` pins lead; the rest keep their scored place
    e = [C("p1", True), C("s1"), C("p2", True), C("s2"), C("p3", True)]
    assert [x.name for x in exact_first(e, limit=1)] == ["p1", "s1", "p2", "s2", "p3"]
    assert [x.name for x in exact_first(e, limit=2)] == ["p1", "p2", "s1", "s2", "p3"]
    assert [x.name for x in exact_first(e, limit=0)] == ["p1", "s1", "p2", "s2", "p3"]
