"""v4.0.1 regression test: the VEC tier must honor the category filter.

Before the fix, /context built VEC chunks without a category, so session_log
(and stale/source) filters silently bypassed every vector hit. This proves a
session_log memory is hidden by default and reachable with exclude_categories=[].
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)

# Fixed 768-dim vector — fake providers embed everything to this, so the query
# matches every stored memory and the metadata filter alone decides what returns.
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
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "topology"
    async def health_check(self): return True


@pytest.fixture
def client(tmp_path):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50098),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


def _writeback(client, summary, category):
    r = client.post("/writeback", json={
        "session_id": f"s-{category}", "summary": summary,
        "key_facts": [], "category": category, "source": "tool",
    })
    assert r.status_code == 200, r.text


def test_vec_session_log_hidden_by_default_and_reachable_explicitly(client):
    _writeback(client, "raw auto-sync session activity dump", "session_log")
    _writeback(client, "artforge runs the mnemo server on port 50001", "topology")

    # Default recall (exclude_categories defaults to ['session_log']) → Tier 1 only
    r = client.post("/context", json={"prompt": "artforge mnemo", "max_results": 5})
    assert r.status_code == 200, r.text
    cats = [c.get("category") for c in r.json()["chunks"]]
    assert "topology" in cats
    assert "session_log" not in cats   # the fix: VEC hit now carries category and is excluded

    # exclude_categories=[] → Tier 1 + Tier 2 (session_log reachable for drill-down)
    r2 = client.post("/context", json={"prompt": "artforge mnemo", "max_results": 5, "exclude_categories": []})
    assert r2.status_code == 200, r2.text
    cats2 = [c.get("category") for c in r2.json()["chunks"]]
    assert "session_log" in cats2


# ── #468: category pushdown + L3 suppression, end-to-end through /context ──

def test_category_request_does_not_fall_through_to_l3(client):
    """When the caller pins a category, the VEC tier serves it — even when the
    result underfills max_results, /context returns the partial set instead of
    the slow L3 disk-walk. Proven by spying on l3_scan: it must NOT be called."""
    _writeback(client, "artforge runs the mnemo server on port 50001", "topology")
    for i in range(4):
        _writeback(client, f"raw auto-sync session activity dump {i}", "session_log")

    with patch("agentb.server.l3_scan", new=AsyncMock(return_value=[])) as l3:
        r = client.post("/context", json={
            "prompt": "artforge mnemo", "max_results": 5, "category": "topology",
        })
    assert r.status_code == 200, r.text
    cats = [c.get("category") for c in r.json()["chunks"]]
    assert cats == ["topology"]          # the one on-category memory, partial set
    assert r.json()["cache_hits"]["L3"] == 0
    l3.assert_not_called()               # the guard: no fall-through to the disk-walk


def test_pinned_category_with_no_vec_hits_still_uses_l3(client, tmp_path):
    """The un-backfilled deploy window: rows exist in vec but their category
    column is NULL (a positive filter matches nothing). A pinned category that
    VEC can't serve must STILL reach L3 — suppressing it here would re-introduce
    the false-negative #468 exists to prevent. (pytest injects the same tmp_path
    into the fixture and the test, so the store lives under it.)"""
    import glob
    from pathlib import Path
    from agentb.vec import VecStore

    _writeback(client, "artforge runs the mnemo server on port 50001", "topology")

    # Simulate the un-backfilled state: null the category column on every vec row.
    dbs = glob.glob(str(tmp_path / "agents" / "*" / "vec_index.sqlite"))
    assert dbs, "expected a vec index under the temp data dir"
    for db in dbs:
        vs = VecStore(Path(db))
        vs._conn.execute("UPDATE vec_sources SET category = NULL")
        vs._conn.commit()
        vs.close()

    with patch("agentb.server.l3_scan", new=AsyncMock(return_value=[])) as l3:
        r = client.post("/context", json={
            "prompt": "artforge mnemo", "max_results": 5, "category": "topology",
        })
    assert r.status_code == 200, r.text
    l3.assert_called()   # VEC served nothing on-category → L3 must remain the escape hatch


def test_default_recall_still_uses_l3_when_cheap_tiers_underfill(client):
    """A plain recall where NOTHING survives the filters (everything is hidden
    session_log) must still reach L3 — zero VEC survivors means the pushdown
    could not serve, so the escape hatch stays open. (v4.9.2 narrowed the old
    'any pinned-category' wording: the hatch now closes whenever a filtered
    kNN produced at least one survivor, exclusion included — see the two
    S120-regression tests below.)"""
    for i in range(3):
        _writeback(client, f"raw auto-sync session activity dump {i}", "session_log")

    with patch("agentb.server.l3_scan", new=AsyncMock(return_value=[])) as l3:
        r = client.post("/context", json={"prompt": "anything", "max_results": 5})
    assert r.status_code == 200, r.text
    l3.assert_called()                   # zero survivors → L3 still the escape hatch


# ── v4.9.2: the S120 boot-timeout regression (23s /context on a 6.2k store) ──

def test_exclusion_underfill_serves_partial_without_l3(client):
    """The default session_log exclusion is a pushed-down filter too. When the
    filtered kNN serves at least one survivor but underfills max_results,
    /context must return the partial set — NOT walk the disk. This was the
    S120 boot timeout: session-flavored prompts on a session_log-dominated
    store underfilled VEC and paid a 23s L3 embed-walk on every recall."""
    _writeback(client, "artforge runs the mnemo server on port 50001", "topology")
    for i in range(6):
        _writeback(client, f"raw auto-sync session activity dump {i}", "session_log")

    with patch("agentb.server.l3_scan", new=AsyncMock(return_value=[])) as l3:
        r = client.post("/context", json={"prompt": "artforge mnemo", "max_results": 5})
    assert r.status_code == 200, r.text
    cats = [c.get("category") for c in r.json()["chunks"]]
    assert "topology" in cats            # the survivor was served
    assert "session_log" not in cats     # exclusion still holds
    l3.assert_not_called()               # partial beats the disk-walk


def test_vec_escalation_retry_fires_once_on_underfill(client):
    """On a filtered underfill the handler re-runs the kNN exactly once with a
    5x wider over-fetch (milliseconds) before accepting the partial set —
    that's the rescue for hidden-category-dominated semantic neighborhoods."""
    from agentb.vec import VecStore

    _writeback(client, "artforge runs the mnemo server on port 50001", "topology")
    for i in range(6):
        _writeback(client, f"raw auto-sync session activity dump {i}", "session_log")

    multipliers = []
    orig = VecStore.search

    def spy(self, *args, **kwargs):
        multipliers.append(kwargs.get("overfetch_multiplier"))
        return orig(self, *args, **kwargs)

    with patch.object(VecStore, "search", spy), \
         patch("agentb.server.l3_scan", new=AsyncMock(return_value=[])):
        r = client.post("/context", json={"prompt": "artforge mnemo", "max_results": 5})
    assert r.status_code == 200, r.text
    assert len(multipliers) == 2                       # one pass + one escalation
    assert multipliers[1] == multipliers[0] * 5        # escalation is 5x wider
