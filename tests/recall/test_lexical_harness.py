"""The lexical lane's harness (v4.21): identifiers.

E2 (test_recall_harness.py) guards the ranker on a world of paraphrase and
decoys. This world is the other failure: a prompt that names an EXACT
identifier — an advisory id, an error string, a commit hash, a file name,
a port, a CVE — and little else, where three memories share the topic and
only the words can pick the answer. Same instrument: production vectors
cached in embeddings.json, the real /context handler, recall@5 + MRR
floored by measured gates in lexical_fixtures.json.

The control is the lane switched OFF on the same queries: it must score
below the gate, or the lane is not doing anything on this world and the
test says so.
"""
from __future__ import annotations

import pytest

from agentb.config import RankingConfig
from tests.recall.embed_fixtures import EMBEDDINGS, cache_key, load_cache, load_lexical_fixtures
from tests.recall.test_recall_harness import (
    REGEN, TOP_K, _make_client, _seed, report, run_harness, summarize,
)


@pytest.fixture(scope="module")
def lexical_world():
    fixtures = load_lexical_fixtures()
    if not EMBEDDINGS.exists():
        pytest.fail(f"{EMBEDDINGS.name} missing — run {REGEN}")
    return fixtures, load_cache()["vectors"]


def test_lexical_fixture_integrity(lexical_world):
    fixtures, vectors = lexical_world
    ids = [m["id"] for m in fixtures["memories"]]
    assert len(ids) == len(set(ids)), "duplicate memory ids"
    for q in fixtures["queries"]:
        missing = [e for e in q["expected"] if e not in ids]
        assert not missing, f"{q['id']} expects unknown ids {missing}"
    for m in fixtures["memories"]:
        assert cache_key("document", m["summary"]) in vectors, f"{m['id']}: no vector — run {REGEN}"
    for q in fixtures["queries"]:
        assert cache_key("query", q["prompt"]) in vectors, f"{q['id']}: no vector — run {REGEN}"


def _run(tmp_path, fixtures, vectors, ranking):
    index_path = _seed(tmp_path, fixtures, vectors)
    with _make_client(tmp_path, vectors, ranking) as client:
        rows = run_harness(client, index_path, fixtures)
    return rows, summarize(rows)


def test_lexical_gate(tmp_path, lexical_world):
    fixtures, vectors = lexical_world
    gate = fixtures["gate"]
    rows, summary = _run(tmp_path, fixtures, vectors, RankingConfig())
    text = report(rows, summary, "lexical harness — lane ON (live default)")
    print("\n" + text)
    assert gate["recall_at_5_min"] is not None and gate["mrr_min"] is not None, (
        "gate unset in lexical_fixtures.json — record the measured baseline before this test can guard anything")
    assert summary["recall_at_5"] >= gate["recall_at_5_min"], (
        f"recall@{TOP_K} {summary['recall_at_5']:.3f} fell below gate {gate['recall_at_5_min']}\n{text}")
    assert summary["mrr"] >= gate["mrr_min"], (
        f"MRR {summary['mrr']:.3f} fell below gate {gate['mrr_min']}\n{text}")


def test_lane_off_scores_below_the_gate(tmp_path, lexical_world):
    """Control: pure vector recall on the identifier world. If this clears
    the gate the lane adds nothing here and its green means nothing."""
    fixtures, vectors = lexical_world
    gate = fixtures["gate"]
    rows, summary = _run(tmp_path, fixtures, vectors, RankingConfig(lexical_enabled=False))
    text = report(rows, summary, "control — lexical lane OFF")
    print("\n" + text)
    assert summary["mrr"] < gate["mrr_min"], (
        f"control MRR {summary['mrr']:.3f} passed the gate {gate['mrr_min']} — "
        f"the vectors already solve this world; the lane is not measured here\n{text}")
