"""4.26.0 proposal envelope — fact_proposals widened in place.

One table for every machine proposal: the locked-fact proposals it already
held (target_kind='fact') plus opinions about a memory (memory_category,
memory_note) keyed by the sentinel entity 'memory:<tenant>/<id>'. Spec:
brain/spec-blend-build1-proposal-envelope.md, acceptance items 2, 3, 4, 7.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.facts_store import FactsStore
from agentb.stick_facts import dump_history
from tests.test_context_authority import FakeEmbedding, FakeReasoning

# Built by concatenation so no secret-shaped literal sits in the file; 18
# chars, long enough for the generic-assignment pattern (16+).
SECRET = "pass" + "word=" + "Zq7" * 6

# The 4.25.4 schema, frozen verbatim from 7dbeb7c (agentb/facts_store.py
# SCHEMA) plus the authority columns every 4.23+ store carries by ALTER.
SCHEMA_4_25 = """
CREATE TABLE IF NOT EXISTS facts (
    entity           TEXT NOT NULL,
    attribute        TEXT NOT NULL,
    value            TEXT NOT NULL,
    confidence       TEXT NOT NULL CHECK(confidence IN ('verified', 'high_probability', 'false')),
    evidence_source  TEXT NOT NULL,
    source_memory_id TEXT,
    source_agent     TEXT,
    created_at       REAL NOT NULL,
    last_updated     REAL NOT NULL,
    PRIMARY KEY (entity, attribute)
);
CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity);
CREATE INDEX IF NOT EXISTS idx_facts_confidence ON facts(confidence);
CREATE INDEX IF NOT EXISTS idx_facts_last_updated ON facts(last_updated);

CREATE TABLE IF NOT EXISTS fact_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity           TEXT NOT NULL,
    attribute        TEXT NOT NULL,
    old_value        TEXT,
    new_value        TEXT,
    old_confidence   TEXT,
    new_confidence   TEXT,
    reason           TEXT NOT NULL,
    changed_at       REAL NOT NULL,
    changed_by       TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_entity_attr ON fact_history(entity, attribute);
CREATE INDEX IF NOT EXISTS idx_history_changed_at ON fact_history(changed_at);

CREATE TABLE IF NOT EXISTS fact_proposals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity            TEXT NOT NULL,
    attribute         TEXT NOT NULL,
    proposed_value    TEXT NOT NULL,
    confidence        TEXT NOT NULL,
    evidence_source   TEXT NOT NULL,
    source_agent      TEXT,
    authority         TEXT NOT NULL,
    current_value     TEXT,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
    seen_count        INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    last_seen         REAL,
    resolved_at       REAL,
    resolved_by       TEXT,
    resolution_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON fact_proposals(status);
ALTER TABLE facts ADD COLUMN authority TEXT NOT NULL DEFAULT 'open';
ALTER TABLE facts ADD COLUMN probe_cmd TEXT;
ALTER TABLE facts ADD COLUMN probe_host TEXT;
"""


def _memory(memory_dir: Path, memory_id: str = "abc123def4567890",
            category: str = "topology", **extra) -> str:
    memory_dir.mkdir(parents=True, exist_ok=True)
    rec = {"id": memory_id, "summary": "IGOR runs the dreamer", "category": category, **extra}
    (memory_dir / f"{memory_id}.json").write_text(json.dumps(rec), encoding="utf-8")
    return memory_id


@pytest.fixture
def store(tmp_path):
    return FactsStore(tmp_path / "facts.sqlite")


def _propose(store, memory_dir, mid, value="decision", quotes=None, score=0.9):
    return store.propose_memory("memory_category", "cc", mid, memory_dir, value,
                                quotes if quotes is not None else ["IGOR runs the dreamer"],
                                score, "jev:shadow:v1", "jev", "topology")


# ── acceptance 2: a 4.25.x store migrates and its rows read as facts ──

def test_425_fixture_migrates_and_old_rows_read_as_fact(tmp_path):
    db = tmp_path / "facts.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_4_25)
    now = time.time()
    conn.execute("INSERT INTO facts (entity, attribute, value, confidence, evidence_source, "
                 "created_at, last_updated, authority) VALUES "
                 "('igor-2', 'role', 'cortex host', 'verified', 'statement:guy', ?, ?, 'declared')",
                 (now, now))
    for i, status in enumerate(("pending", "rejected")):
        conn.execute("INSERT INTO fact_proposals (entity, attribute, proposed_value, confidence, "
                     "evidence_source, source_agent, authority, current_value, status, created_at) "
                     "VALUES ('igor-2', 'role', ?, 'high_probability', 'dreamer', 'cc', 'declared', "
                     "'cortex host', ?, ?)", (f"value {i}", status, now + i))
    conn.commit()
    conn.close()

    store = FactsStore(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(fact_proposals)")}
    assert {"target_kind", "target_ref", "evidence_quotes", "confidence_score", "source_run"} <= cols
    idx = {r[1] for r in sqlite3.connect(db).execute("PRAGMA index_list(fact_proposals)")}
    assert "idx_proposals_target" in idx
    rows = store.proposals(status=None, kind="fact")
    assert len(rows) == 2
    assert {r["target_kind"] for r in rows} == {"fact"}
    assert all(r["evidence_quotes"] == [] and r["confidence_score"] is None for r in rows)
    # the migrated store still holds and writes through its lock
    assert store.get("igor-2", "role").value == "cortex host"
    res = store.save("igor-2", "role", "something else", "high_probability", "dreamer")
    assert res.proposal_id is not None and not res.written
    # opening twice is a no-op (idempotent ALTER + index)
    FactsStore(db)


# ── acceptance 3: propose_memory refuses bad input and redacts quotes ──

def test_propose_memory_refuses_empty_quotes(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    with pytest.raises(ValueError, match="at least one quote"):
        _propose(store, tmp_path / "mem", mid, quotes=[])


@pytest.mark.parametrize("kwargs,match", [
    ({"quotes": ["x" * 501]}, "over 500"),
    ({"score": 1.5}, "between 0 and 1"),
    ({"score": -0.1}, "between 0 and 1"),
    ({"value": "not-a-category"}, "not a category"),
])
def test_propose_memory_refuses_bad_input(store, tmp_path, kwargs, match):
    mid = _memory(tmp_path / "mem")
    with pytest.raises(ValueError, match=match):
        _propose(store, tmp_path / "mem", mid, **kwargs)


def test_propose_memory_refuses_unknown_kind_and_missing_memory(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    with pytest.raises(ValueError, match="target_kind"):
        store.propose_memory("fact", "cc", mid, tmp_path / "mem", "decision", ["q"], 0.5, "t")
    with pytest.raises(ValueError, match="no memory"):
        _propose(store, tmp_path / "mem", "0000000000000000")


def test_propose_memory_redacts_quote(store, tmp_path):
    """Wiring, not threshold: proves stored quotes pass through redact_text.
    Uses an 18-char secret because the redactor's generic-assignment floor
    is 16 chars — a short `password=abc123` passes today (known gap,
    snag-redactor-short-kv-secret-passes.md, batch 2 leftover #4)."""
    mid = _memory(tmp_path / "mem")
    pid, seen = _propose(store, tmp_path / "mem", mid, quotes=[f"login with {SECRET} ok"])
    assert seen == 1
    row = store.proposals(kind="memory_category")[0]
    assert row["id"] == pid
    assert "[REDACTED:" in row["evidence_quotes"][0]
    assert SECRET not in json.dumps(row)
    raw = sqlite3.connect(store.path).execute("SELECT * FROM fact_proposals").fetchall()
    assert SECRET not in repr(raw)


def test_memory_note_value_is_redacted(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    store.propose_memory("memory_note", "cc", mid, tmp_path / "mem", f"see {SECRET}",
                         ["q"], 0.5, "analyst:lens")
    row = store.proposals(kind="memory_note")[0]
    assert SECRET not in row["proposed_value"] and "[REDACTED:" in row["proposed_value"]


# ── acceptance 4: dedupe — pending bumps, a rejection holds for 30 days ──

def test_identical_disagreement_bumps_seen_count(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    p1, s1 = _propose(store, tmp_path / "mem", mid)
    p2, s2 = _propose(store, tmp_path / "mem", mid, score=0.95)
    assert (p1, s1) == (p1, 1) and (p2, s2) == (p1, 2)
    rows = store.proposals(kind="memory_category")
    assert len(rows) == 1 and rows[0]["seen_count"] == 2 and rows[0]["confidence_score"] == 0.95
    assert rows[0]["entity"] == f"memory:cc/{mid}" and rows[0]["attribute"] == "category"
    assert rows[0]["target_ref"] == f"memory:cc/{mid}"
    # a different proposed value is a different proposal
    p3, s3 = _propose(store, tmp_path / "mem", mid, value="incident")
    assert p3 != p1 and s3 == 1


def test_rejected_blocks_refile_for_30_days(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    pid, _ = _propose(store, tmp_path / "mem", mid)
    assert store.resolve_proposal(pid, "reject", "cc", "Guy: it is topology").reason.endswith("rejected")
    again, seen = _propose(store, tmp_path / "mem", mid)
    assert (again, seen) == (pid, 0)                     # held by the rejection
    assert len(store.proposals(status=None, kind="memory_category")) == 1


def test_rejected_refile_allowed_on_day_31(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    pid, _ = _propose(store, tmp_path / "mem", mid)
    store.resolve_proposal(pid, "reject", "cc", "no")
    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE fact_proposals SET resolved_at=? WHERE id=?",
                 (time.time() - 31 * 86400, pid))
    conn.commit()
    conn.close()
    new, seen = _propose(store, tmp_path / "mem", mid)
    assert new != pid and seen == 1


def test_fact_kind_dedupe_unchanged_by_memory_rows(store):
    """A memory row never collapses onto a fact row, and fact rejections
    keep today's rule (no 30-day hold for facts — pinned by acceptance 1)."""
    store.save("igor", "role", "a", "verified", "statement:guy")
    store.set_authority("igor", "role", "declared", "statement:guy")
    r1 = store.save("igor", "role", "b", "high_probability", "dreamer", source_agent="dreamer")
    store.resolve_proposal(r1.proposal_id, "reject", "cc", "no")
    r2 = store.save("igor", "role", "b", "high_probability", "dreamer", source_agent="dreamer")
    assert r2.proposal_id != r1.proposal_id
    row = [r for r in store.proposals(kind="fact") if r["id"] == r2.proposal_id][0]
    assert row["evidence_quotes"] == ["dreamer"] and row["source_run"] == "dreamer"


# ── D1: a memory sentinel never surfaces as a fact ──

def test_memory_proposal_never_surfaces_as_a_fact(store, tmp_path):
    mid = _memory(tmp_path / "mem")
    pid, _ = _propose(store, tmp_path / "mem", mid)
    store.resolve_proposal(pid, "reject", "cc", "no")
    assert store.query(limit=100) == []
    assert store.get(f"memory:cc/{mid}", "category") is None
    assert store.locked_for_prompt(f"memory cc {mid} category") == []
    # memory audit reasons ("memory_category proposal #N …") never match the
    # contradictions filter's prefixes; this pins that
    assert store.contradictions() == []
    assert store.proposals(status=None, kind="fact") == []
    # the stick carries fact audit rows between hosts; a memory's stays home
    assert [r for r in dump_history(store.path) if r["entity"].startswith("memory:")] == []
    hist = sqlite3.connect(store.path).execute(
        "SELECT COUNT(*) FROM fact_history WHERE entity LIKE 'memory:%'").fetchone()[0]
    assert hist == 2       # proposal + rejection rows exist, locally


# ── HTTP: acceptance 7 + the routes ──

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMO_JEV_KEY_FILE", raising=False)
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c, tmp_path


def _writeback(c, summary="IGOR runs the dreamer nightly", category="topology", agent="cc", **extra):
    r = c.post("/writeback", json={"session_id": "s-env", "summary": summary, "key_facts": [],
                                   "category": category, "source": "inferred", "force": True,
                                   "agent_id": agent, **extra})
    assert r.status_code == 200, r.text
    return r.json()["memory_id"]


def _memory_path(tmp_path, mid):
    hits = list(Path(tmp_path).rglob(f"{mid}.json"))
    assert len(hits) == 1, hits
    return hits[0]


def _vec_category(tmp_path, mid):
    db = _memory_path(tmp_path, mid).parent.parent / "vec_index.sqlite"
    return sqlite3.connect(db).execute(
        "SELECT category FROM vec_sources WHERE memory_id=?", (mid,)).fetchone()[0]


def _post_proposal(c, mid, value="decision", **extra):
    body = {"target_kind": "memory_category", "agent_id": "cc", "memory_id": mid,
            "proposed_value": value, "evidence_quotes": ["IGOR runs the dreamer"],
            "confidence_score": 0.93, "source_run": "jev:shadow:v1", "source_agent": "jev", **extra}
    return c.post("/proposals", json=body)


def test_accept_memory_category_amends_atomically(client):
    c, tmp = client
    mid = _writeback(c)
    path = _memory_path(tmp, mid)
    before = json.loads(path.read_text(encoding="utf-8"))
    assert _vec_category(tmp, mid) == "topology"
    r = _post_proposal(c, mid)
    assert r.status_code == 200, r.text
    pid = r.json()["proposal_id"]

    with patch("agentb.server._atomic_write_text", wraps=__import__(
            "agentb.server", fromlist=["_atomic_write_text"])._atomic_write_text) as aw:
        r = c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc",
                                                      "reason": "Guy: yes, a decision"})
    assert r.status_code == 200, r.text
    assert r.json()["written"] is True and r.json()["previous_value"] == "topology"
    assert any(call.args[0] == path for call in aw.call_args_list)   # the atomic writer did it

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["category"] == "decision"
    assert after["category_original"] == "topology"
    assert after["classified_by"] == f"proposal:#{pid}"
    for k in ("summary", "key_facts", "timestamp", "id", "session_id"):
        assert after.get(k) == before.get(k)                 # testimony untouched
    assert _vec_category(tmp, mid) == "decision"             # recall pre-filter (#468)
    hist = FactsStore(tmp / "facts.sqlite").history(f"memory:cc/{mid}", "category")
    assert any(f"#{pid} accepted" in h["reason"] and h["new_value"] == "decision" for h in hist)
    v = c.get("/ledger/verify", params={"agent_id": "cc"}).json()
    assert v["ok"] and v["chain"] == "intact" and v["sealed"] >= 1
    assert v["altered"] == [] and v["unsealed"] == []        # no re-seal needed (§2.3)
    assert c.get("/proposals", params={"kind": "memory_category"}).json()["count"] == 0


def test_second_accept_keeps_the_first_category_original(client):
    c, tmp = client
    mid = _writeback(c)
    for value in ("decision", "incident"):
        pid = _post_proposal(c, mid, value=value).json()["proposal_id"]
        assert c.post(f"/proposals/{pid}/resolve",
                      json={"action": "accept", "by": "cc", "reason": "Guy"}).json()["written"]
    after = json.loads(_memory_path(tmp, mid).read_text(encoding="utf-8"))
    assert after["category"] == "incident" and after["category_original"] == "topology"


def test_accept_clears_needs_reclassification(client):
    c, tmp = client
    mid = _writeback(c)
    path = _memory_path(tmp, mid)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["needs_reclassification"] = True
    rec["classified_by"] = "regex"
    path.write_text(json.dumps(rec), encoding="utf-8")
    pid = _post_proposal(c, mid).json()["proposal_id"]
    c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc", "reason": "Guy"})
    after = json.loads(path.read_text(encoding="utf-8"))
    assert "needs_reclassification" not in after
    assert after["classified_by"] == f"proposal:#{pid}"


def test_accept_failure_leaves_proposal_pending(client):
    c, tmp = client
    mid = _writeback(c)
    pid = _post_proposal(c, mid).json()["proposal_id"]
    _memory_path(tmp, mid).unlink()
    r = c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc", "reason": "Guy"})
    assert r.status_code == 400 and "no memory" in r.text
    assert c.get("/proposals", params={"kind": "memory_category"}).json()["count"] == 1


def test_reject_memory_category_leaves_memory_untouched(client):
    c, tmp = client
    mid = _writeback(c)
    before = _memory_path(tmp, mid).read_text(encoding="utf-8")
    pid = _post_proposal(c, mid).json()["proposal_id"]
    r = c.post(f"/proposals/{pid}/resolve", json={"action": "reject", "by": "cc", "reason": "no"})
    assert r.status_code == 200 and r.json()["written"] is False
    assert _memory_path(tmp, mid).read_text(encoding="utf-8") == before
    again = _post_proposal(c, mid).json()
    assert again == {"proposal_id": pid, "seen_count": 0, "status": "held_by_rejection",
                     "held_by_rejection": True}


def test_memory_note_accept_not_promotable(client):
    c, _ = client
    mid = _writeback(c)
    r = c.post("/proposals", json={"target_kind": "memory_note", "agent_id": "cc", "memory_id": mid,
                                   "proposed_value": "links to S357", "evidence_quotes": ["q"],
                                   "confidence_score": 0.5, "source_run": "analyst:lens"})
    pid = r.json()["proposal_id"]
    r = c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc", "reason": "Guy"})
    assert r.json()["reason"] == "kind not promotable yet" and r.json()["written"] is False
    assert c.get("/proposals", params={"kind": "memory_note"}).json()["count"] == 1


def test_proposals_endpoints_kind_filter(client):
    c, _ = client
    mid = _writeback(c)
    assert _post_proposal(c, mid).json()["status"] == "new"
    assert _post_proposal(c, mid).json()["status"] == "repeat"
    body = c.get("/proposals").json()
    assert body["count"] == 1 and body["kind"] == "all"
    assert body["pending_by_kind"] == {"fact": 0, "memory_category": 1, "memory_note": 0}
    assert c.get("/proposals", params={"kind": "fact"}).json()["count"] == 0
    assert c.get("/facts/proposals").json()["count"] == 0      # facts only, as before
    assert c.get("/proposals", params={"kind": "bogus"}).status_code == 400
    assert c.post("/proposals", json={"target_kind": "fact", "memory_id": mid, "proposed_value": "x",
                                      "evidence_quotes": ["q"], "confidence_score": 0.5,
                                      "source_run": "t"}).status_code == 400
    assert _post_proposal(c, "../etc").status_code == 400
    assert _post_proposal(c, mid, evidence_quotes=[]).status_code == 400


def test_proposals_routes_are_not_scopable():
    from agentb.config import SCOPABLE_ENDPOINTS
    assert not any(e.startswith("/proposals") for e in SCOPABLE_ENDPOINTS)


# ── dream brief: one proposals block, grouped by kind (§2.5, §2.4 line) ──

def test_proposals_block_groups_by_kind(monkeypatch):
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
    spec = importlib.util.spec_from_file_location("mnemo_dream_env", path)
    dream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dream)

    rows = [
        {"id": 7, "target_kind": "memory_category", "target_ref": "memory:cc/abc", "attribute": "category",
         "current_value": "topology", "proposed_value": "decision", "confidence_score": 0.93,
         "source_run": "jev:shadow:v1", "evidence_quotes": ["We chose SQLite"], "seen_count": 1},
        {"id": 3, "target_kind": "fact", "entity": "igor-2", "attribute": "role", "authority": "declared",
         "source_agent": "cc", "proposed_value": "x", "current_value": "y",
         "evidence_source": "dreamer", "seen_count": 1},
    ]
    stats = {"enabled": True, "days": []}

    class R:
        def __init__(self, body): self.status_code, self._b = 200, body
        def json(self): return self._b

    def fake_get(url, **kw):
        if url.endswith("/proposals"):
            kind = kw["params"]["kind"]
            got = [r for r in rows if kind == "all" or r["target_kind"] == kind]
            return R({"proposals": got, "count": len(got),
                      "pending_by_kind": {"fact": 1, "memory_category": 1, "memory_note": 0}})
        assert url.endswith("/proposals/jev-stats")
        return R(stats)

    monkeypatch.setattr(dream.httpx, "get", fake_get)
    lines = dream.proposals_block().splitlines()
    assert lines[0] == "### Pending proposals"
    assert lines[1].startswith("Pending proposals: 1 fact · 1 category")
    assert lines[2].startswith("jev shadow (")                 # second line survives a trim
    assert lines[3].startswith("#3 igor-2.role") and lines[4].startswith("#7 memory:cc/abc category")
    assert "topology -> decision (0.93) by jev:shadow:v1" in lines[4]
    assert not any("locked facts" in ln for ln in lines)      # replaced, not added
    stats["enabled"] = False
    rows.clear()
    lines = dream.proposals_block().splitlines()
    assert lines[1] == "Pending proposals: 1 fact · 1 category. Nothing waits on Guy's word."
    assert lines[2] == "jev shadow: OFF"


def _load_dream():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
    spec = importlib.util.spec_from_file_location("mnemo_dream_env2", path)
    dream = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dream)
    return dream


class _Resp:
    def __init__(self, body, status=200): self.status_code, self._b = status, body
    def json(self): return self._b


def test_brief_keeps_locked_facts_when_jev_rows_flood(monkeypatch):
    """Review R3: twelve newer Jev rows must not push the one contested
    locked fact out of the ten-row block."""
    dream = _load_dream()
    fact = {"id": 1, "target_kind": "fact", "entity": "igor-2", "attribute": "role",
            "authority": "declared", "proposed_value": "x", "current_value": "y",
            "evidence_source": "dreamer", "seen_count": 1, "created_at": 1.0}
    jev = [{"id": 100 + i, "target_kind": "memory_category", "target_ref": f"memory:cc/m{i}",
            "attribute": "category", "current_value": "topology", "proposed_value": "decision",
            "confidence_score": 0.9, "source_run": "jev:shadow:v1", "evidence_quotes": ["q"],
            "seen_count": 1, "created_at": 100.0 + i} for i in range(12)]
    newest_first = sorted(jev + [fact], key=lambda r: -r["created_at"])

    def fake_get(url, **kw):
        if url.endswith("/jev-stats"):
            return _Resp({"enabled": True, "days": []})
        kind, limit = kw["params"]["kind"], kw["params"]["limit"]
        got = [r for r in newest_first if kind == "all" or r["target_kind"] == kind][:limit]
        return _Resp({"proposals": got, "count": len(got),
                      "pending_by_kind": {"fact": 1, "memory_category": 12, "memory_note": 0}})

    monkeypatch.setattr(dream.httpx, "get", fake_get)
    lines = dream.proposals_block(limit=10).splitlines()
    rows = [ln for ln in lines if ln.startswith("#") and not ln.startswith("###")]
    assert len(rows) == 10
    assert rows[0].startswith("#1 igor-2.role")


def test_brief_counts_unknown_without_pending_by_kind(monkeypatch):
    dream = _load_dream()

    def fake_get(url, **kw):
        if url.endswith("/jev-stats"):
            return _Resp({"enabled": False})
        return _Resp({"proposals": [], "count": 0})          # an older shape: no pending_by_kind

    monkeypatch.setattr(dream.httpx, "get", fake_get)
    lines = dream.proposals_block().splitlines()
    assert "UNKNOWN" in lines[1] and "Nothing waits" not in lines[1]


def test_vec_update_runs_on_the_loop_thread(client):
    """Review R1: the accept's vec write shares the one VecStore connection
    with writeback upserts, so it must run on the same (loop) thread."""
    import threading
    from agentb.vec import VecStore
    c, _ = client
    seen = {}
    real_upsert, real_update = VecStore.upsert, VecStore.update_category

    def upsert(self, *a, **k):
        seen["upsert"] = threading.get_ident()
        return real_upsert(self, *a, **k)

    def update(self, *a, **k):
        seen["update"] = threading.get_ident()
        return real_update(self, *a, **k)

    with patch.object(VecStore, "upsert", upsert), patch.object(VecStore, "update_category", update):
        mid = _writeback(c)
        pid = _post_proposal(c, mid).json()["proposal_id"]
        r = c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc", "reason": "Guy"})
    assert r.status_code == 200 and r.json()["written"]
    assert seen["update"] == seen["upsert"]


_FAIL_CLOSE = ("CREATE TRIGGER fail_close BEFORE INSERT ON fact_history "
               "WHEN NEW.entity LIKE 'memory:%' AND NEW.reason LIKE '%accepted%' "
               "BEGIN SELECT RAISE(ABORT, 'close failed'); END;")


def test_resolve_calls_undo_when_the_close_fails(store, tmp_path):
    """Silent-failure #3: the memory moved but the proposal could not
    close — undo() must run and the proposal must stay pending."""
    mid = _memory(tmp_path / "mem")
    pid, _ = _propose(store, tmp_path / "mem", mid)
    conn = sqlite3.connect(store.path)
    conn.execute(_FAIL_CLOSE)
    conn.commit()
    conn.close()
    undone = []
    with pytest.raises(sqlite3.DatabaseError):
        store.resolve_proposal(pid, "accept", "cc", "Guy",
                               lambda *a: ("topology", lambda: undone.append(True)))
    assert undone == [True]
    assert store.proposals(kind="memory_category")[0]["status"] == "pending"


def test_accept_restores_the_file_when_the_close_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMO_JEV_KEY_FILE", raising=False)
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS))
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg), raise_server_exceptions=False) as c:
            mid = _writeback(c)
            path = _memory_path(tmp_path, mid)
            before = path.read_text(encoding="utf-8")
            pid = _post_proposal(c, mid).json()["proposal_id"]
            conn = sqlite3.connect(tmp_path / "facts.sqlite")
            conn.execute(_FAIL_CLOSE)
            conn.commit()
            conn.close()
            r = c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc", "reason": "Guy"})
            assert r.status_code == 500
            assert path.read_text(encoding="utf-8") == before       # file put back
            assert _vec_category(tmp_path, mid) == "topology"       # vec never moved
            assert c.get("/proposals", params={"kind": "memory_category"}).json()["count"] == 1


def test_failed_undo_is_logged(store, tmp_path, caplog):
    """Confirming review #1: if undo() itself fails, the log must say the
    file and the proposal now disagree; the original error still raises."""
    import logging
    mid = _memory(tmp_path / "mem")
    pid, _ = _propose(store, tmp_path / "mem", mid)
    conn = sqlite3.connect(store.path)
    conn.execute(_FAIL_CLOSE)
    conn.commit()
    conn.close()

    def bad_undo():
        raise OSError("disk gone")
    with caplog.at_level(logging.ERROR), pytest.raises(sqlite3.DatabaseError):
        store.resolve_proposal(pid, "accept", "cc", "Guy", lambda *a: ("topology", bad_undo))
    assert "close failed AND undo failed" in caplog.text
