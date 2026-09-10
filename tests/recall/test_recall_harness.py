"""E2 — the recall harness: the gate for every ranker change.

Experiment One (S290) found the composite ranker's similarity term inert and
fixed it; it also showed that a fix tuned on the ten results you can see is
tuned on the wrong pool. This harness is the instrument that was missing:
a fixed set of fictional memories with known right answers, embedded by the
production embedder (vectors cached in embeddings.json, see
embed_fixtures.py), served through the real /context handler, scored by
recall@5 and MRR, and floored by measured gates in fixtures.json.

Every query runs against the fixture's access counts, not the counts the
previous query's serving left behind — queries are independent trials.

A control proves the gate can fail: with similarity weighted to zero the
same fixtures must score BELOW the gate. A check that cannot fail is not a
check.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, CacheConfig, ClassificationConfig, ProviderConfig,
    RankingConfig, ResilientProviderConfig, ServerConfig, DEFAULT_PERSONAS,
)
from agentb.vec import EMBED_DIM, VecStore
from tests.recall.embed_fixtures import (
    CACHE_VERSION, EMBED_MODEL, EMBEDDINGS, cache_key, load_cache, load_fixtures,
)

TOP_K = 5
REGEN = "python tests/recall/embed_fixtures.py"
_STATUS = {"primary": "cached", "active": "cached", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class CachedEmbedding:
    """Serves the pre-computed production vectors. A miss is a broken
    fixture set, not a reason to fall back — fail and name the fix."""
    active_label = f"cached/{EMBED_MODEL}"

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    @property
    def status(self):
        return _STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        vec = self._vectors.get(cache_key(task_type, text))
        if vec is None:
            raise RuntimeError(
                f"no cached vector for {task_type} text {text[:60]!r} — run {REGEN}")
        return list(vec)

    async def health_check(self):
        return True


class FakeReasoning:
    active_label = "fake/reason"

    @property
    def status(self):
        return _STATUS

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return "unknown"

    async def health_check(self):
        return True


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world():
    fixtures = load_fixtures()
    if not EMBEDDINGS.exists():
        pytest.fail(f"{EMBEDDINGS.name} missing — run {REGEN}")
    cache = load_cache()
    assert cache["model"] == EMBED_MODEL, (
        f"embedding cache built by {cache['model']!r}, harness expects {EMBED_MODEL!r} — run {REGEN} --all")
    assert cache.get("cache_version") == CACHE_VERSION, (
        f"embedding cache is v{cache.get('cache_version')}, harness expects v{CACHE_VERSION} — run {REGEN} --all")
    assert cache["dim"] == EMBED_DIM, f"cache dim {cache['dim']} != EMBED_DIM {EMBED_DIM}"
    return fixtures, cache["vectors"]


def _make_client(tmp_path: Path, vectors: dict, ranking: RankingConfig):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model=EMBED_MODEL)),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        ranking=ranking,
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=CachedEmbedding(vectors)), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        return TestClient(create_app(cfg))


def _seed(tmp_path: Path, fixtures: dict, vectors: dict) -> Path:
    """Write every fixture memory as JSON + vec row, the way /writeback would,
    with the fixture's age instead of 'now'. Returns the vec index path."""
    base = tmp_path / "agents" / "default"
    mem_dir = base / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    store = VecStore(base / "vec_index.sqlite")
    try:
        for m in fixtures["memories"]:
            created = now - m["age_days"] * 86400.0
            path = mem_dir / f"{m['id']}.json"
            path.write_text(json.dumps({
                "id": m["id"], "summary": m["summary"], "key_facts": [],
                "category": m["category"], "source": "user", "created_at": created,
            }), encoding="utf-8")
            store.upsert(m["id"], m["summary"], vectors[cache_key("document", m["summary"])],
                         source_file=path.as_posix(), created_at=created, category=m["category"])
    finally:
        store.close()
    return base / "vec_index.sqlite"


def _reset_access(index_path: Path, fixtures: dict) -> None:
    """Restore recall_stats to the fixture's access counts. Serving bumps the
    counts, so without this each query would inherit the previous query's
    serving history and the trials would stop being independent."""
    with closing(sqlite3.connect(index_path)) as conn, conn:
        conn.execute("DELETE FROM recall_stats")
        conn.executemany(
            "INSERT INTO recall_stats(memory_id, access_count, last_accessed) VALUES (?, ?, ?)",
            [(m["id"], m["access_count"], time.time())
             for m in fixtures["memories"] if m["access_count"]],
        )


# ── scoring ─────────────────────────────────────────────────────────────────

def run_harness(client: TestClient, index_path: Path, fixtures: dict) -> list[dict]:
    rows = []
    for q in fixtures["queries"]:
        _reset_access(index_path, fixtures)
        r = client.post("/context", json={"prompt": q["prompt"], "max_results": TOP_K})
        assert r.status_code == 200, f"{q['id']}: {r.text}"
        served = [c["memory_id"] for c in r.json()["chunks"]]
        expected = q["expected"]
        ranks = [i + 1 for i, mid in enumerate(served) if mid in expected]
        rows.append({
            "id": q["id"],
            "recall": len(ranks) / len(expected),
            "rr": 1.0 / ranks[0] if ranks else 0.0,
            "served": served,
            "expected": expected,
            "note": q["note"],
            "hard": bool(q.get("hard")),
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    hard = [r for r in rows if r["hard"]]
    return {
        "queries": n,
        "recall_at_5": sum(r["recall"] for r in rows) / n,
        "mrr": sum(r["rr"] for r in rows) / n,
        "top1": sum(1 for r in rows if r["rr"] == 1.0) / n,
        "misses": [r["id"] for r in rows if r["recall"] < 1.0],
        # The discriminating subset (fixtures.json: hard=true). Averaged over
        # 35 queries a rank move on one of these is noise; over the subset it
        # is the signal. See _about in fixtures.json.
        "hard_n": len(hard),
        "hard_mrr": sum(r["rr"] for r in hard) / len(hard) if hard else None,
    }


def report(rows: list[dict], summary: dict, title: str) -> str:
    lines = [f"── {title} ──",
             f"recall@{TOP_K} {summary['recall_at_5']:.3f}   MRR {summary['mrr']:.3f}   "
             f"top-1 {summary['top1']:.0%}   n={summary['queries']}   "
             + (f"hard MRR {summary['hard_mrr']:.3f} (n={summary['hard_n']})"
                if summary["hard_mrr"] is not None else "hard subset: NONE MARKED")]
    for r in rows:
        flag = " " if r["recall"] == 1.0 else "✗"
        mark = "H" if r["hard"] else " "
        lines.append(f"{flag}{mark}{r['id']}  rr={r['rr']:.2f}  served={r['served']}  want={r['expected']}  {r['note']}")
    return "\n".join(lines)


# ── the gate ────────────────────────────────────────────────────────────────

def test_fixture_integrity(world):
    fixtures, vectors = world
    ids = [m["id"] for m in fixtures["memories"]]
    assert len(ids) == len(set(ids)), "duplicate memory ids"
    for q in fixtures["queries"]:
        missing = [e for e in q["expected"] if e not in ids]
        assert not missing, f"{q['id']} expects unknown ids {missing}"
    for m in fixtures["memories"]:
        assert cache_key("document", m["summary"]) in vectors, f"{m['id']}: no vector — run {REGEN}"
    for q in fixtures["queries"]:
        assert cache_key("query", q["prompt"]) in vectors, f"{q['id']}: no vector — run {REGEN}"


def test_recall_gate(tmp_path, world):
    fixtures, vectors = world
    gate = fixtures["gate"]
    index_path = _seed(tmp_path, fixtures, vectors)
    with _make_client(tmp_path, vectors, RankingConfig()) as client:
        rows = run_harness(client, index_path, fixtures)
    summary = summarize(rows)
    text = report(rows, summary, "E2 recall harness — live ranker")
    print("\n" + text)

    assert gate["recall_at_5_min"] is not None and gate["mrr_min"] is not None, (
        "gate unset in fixtures.json — record the measured baseline before this test can guard anything")
    assert summary["recall_at_5"] >= gate["recall_at_5_min"], (
        f"recall@{TOP_K} {summary['recall_at_5']:.3f} fell below gate {gate['recall_at_5_min']}\n{text}")
    assert summary["mrr"] >= gate["mrr_min"], (
        f"MRR {summary['mrr']:.3f} fell below gate {gate['mrr_min']}\n{text}")
    assert gate.get("hard_mrr_min") is not None and summary["hard_n"] > 0, (
        "hard-subset gate unset — mark the discriminating queries hard=true and record the floor")
    assert summary["hard_mrr"] >= gate["hard_mrr_min"], (
        f"hard-subset MRR {summary['hard_mrr']:.3f} fell below gate {gate['hard_mrr_min']} — "
        f"a regression on the discriminating queries, masked by the easy majority\n{text}")


def _demote_first_hit(row: dict) -> dict:
    """The same served list with the first expected id pushed down one slot,
    staying inside the served window. A miss stays a miss and a hit at the
    bottom slot stays put: pushing it out would be a lost query, which the
    primary recall floor already catches — the hard gate is for the
    regression that keeps every answer in the window and still degrades.
    A hit at rank 1 also stays put since the v4.21 re-baseline — for an
    arithmetic reason, stated plainly: the hard subset grew to 7 (q14, the
    multi-hit query, sits at rank 1), and demoting all 7 costs the primary
    MRR 0.036, more than its one-query headroom of 0.029, so the specimen
    tripped the primary gate too and stopped discriminating. Demoting only
    the already-buried answers (rank 2 to 3, 5 to 6) costs the primary 0.021
    and the hard subset 0.107: primary green with 0.008 to spare, hard red.
    That margin is a quarter of one rank step — the NEXT re-baseline must
    re-derive this control, not nudge the predicate again.
    Synthetic, because that regression has not happened yet."""
    served = list(row["served"])
    idx = next((i for i, m in enumerate(served) if m in row["expected"]), None)
    if idx is not None and idx > 0 and idx + 1 < len(served):
        served[idx], served[idx + 1] = served[idx + 1], served[idx]
    ranks = [i + 1 for i, mid in enumerate(served) if mid in row["expected"]]
    return {**row, "served": served,
            "recall": len(ranks) / len(row["expected"]),
            "rr": 1.0 / ranks[0] if ranks else 0.0}


def test_hard_gate_catches_a_regression_the_primary_gate_hides(tmp_path, world):
    """Control for the hard-subset floor specifically: demote every hard
    query whose answer is already below rank 1 by one more rank inside the
    served window and leave the easy ones alone (on the v4.21 baseline that
    is six of the seven; q14's answers sit at ranks 1 and 3).
    The primary floors (one whole query of headroom over 35) must still
    PASS — that is the masking — and the hard floor must FAIL. If the
    primary gate also fails here, the hard gate is redundant and this test
    says so."""
    fixtures, vectors = world
    gate = fixtures["gate"]
    index_path = _seed(tmp_path, fixtures, vectors)
    with _make_client(tmp_path, vectors, RankingConfig()) as client:
        rows = run_harness(client, index_path, fixtures)
    demoted = [_demote_first_hit(r) if r["hard"] else r for r in rows]
    summary = summarize(demoted)
    print("\n" + report(demoted, summary, "control — every hard query one rank lower"))
    assert summary["recall_at_5"] >= gate["recall_at_5_min"] and summary["mrr"] >= gate["mrr_min"], (
        f"primary gate ALSO failed ({summary['recall_at_5']:.3f} / {summary['mrr']:.3f}) — "
        "the hard gate is not adding anything; re-derive its headroom")
    assert summary["hard_mrr"] < gate["hard_mrr_min"], (
        f"hard-subset MRR {summary['hard_mrr']:.3f} passed the gate {gate['hard_mrr_min']} "
        "with every hard query demoted — the hard gate cannot catch the regression it was built for")


def test_gate_can_fail_when_similarity_is_removed(tmp_path, world):
    """Control: the same fixtures under a ranker that ignores similarity must
    score below the gate. If this passes the gate, the harness is not
    measuring ranking and its green means nothing."""
    fixtures, vectors = world
    gate = fixtures["gate"]
    blind = RankingConfig(w_similarity=0.0, w_recency=0.5, w_importance=0.3, w_access=0.2)
    index_path = _seed(tmp_path, fixtures, vectors)
    with _make_client(tmp_path, vectors, blind) as client:
        rows = run_harness(client, index_path, fixtures)
    summary = summarize(rows)
    print("\n" + report(rows, summary, "control — similarity weight 0"))
    assert summary["recall_at_5"] < gate["recall_at_5_min"], (
        f"control recall@{TOP_K} {summary['recall_at_5']:.3f} passed the gate — the harness cannot fail")
    assert summary["mrr"] < gate["mrr_min"], (
        f"control MRR {summary['mrr']:.3f} passed the gate {gate['mrr_min']} — the harness cannot fail")
    assert summary["hard_mrr"] < gate["hard_mrr_min"], (
        f"control hard-subset MRR {summary['hard_mrr']:.3f} passed the gate {gate['hard_mrr_min']} — "
        "the hard gate cannot fail")
