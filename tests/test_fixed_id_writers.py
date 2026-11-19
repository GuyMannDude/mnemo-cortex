"""Item 17 (a)+(b): fixed-id writers and demote.

(b) Every memory-record writer reads, changes and writes under the record's
lock (fsutil.update_record / vec.write_unless_superseded), and the three
fixed-id writers (writeback, Analyst/Muse notes, archived sessions) re-check
the record right before their vec upsert. A demote that lands anywhere in a
writer's sequence ends with the record stamped AND no vector.
(a) The archived-session writer gets item 16's check: a demoted archived id
is not rewritten, sealed or re-indexed.
R2: a supersede mark keeps a record's first stamp. R3: a proposal's undo()
restores only the four fields apply() set.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentb.analyst import analyze_tenant
from agentb.config import AnalysisConfig
from agentb.vec import VecStore

from tests.test_analyst import NOTE_REPLY, VEC_A, ScriptedEmbedder, ScriptedReasoner, _seed_log
from tests.test_ledger import _save, client  # noqa: F401 (fixture)
from tests.test_m_group_server import (
    FakeEmbedding, MASTER, VerdictReasoning, _auth, make_app,
)


def _stamp(path: Path, by="test"):
    """A demote's record half, written the way any version of the code
    leaves it on disk."""
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["superseded_by"] = f"demoted:{by}:{int(time.time())}"
    rec["superseded_at"] = time.time()
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")


# ── the lock ──

def test_two_spellings_of_one_record_share_a_lock(tmp_path):
    from agentb.fsutil import record_lock
    d = tmp_path / "memory"; d.mkdir()
    (tmp_path / "sub").mkdir()
    spellings = [d / "m1.json", tmp_path / "sub" / ".." / "memory" / "m1.json"]
    if os.name != "nt":
        (tmp_path / "link").symlink_to(d, target_is_directory=True)
        spellings.append(tmp_path / "link" / "m1.json")
    locks = {id(record_lock(p)) for p in spellings}
    assert len(locks) == 1


def test_a_writer_under_another_spelling_waits_and_both_changes_land(tmp_path):
    """Forced interleaving: A holds the record (mutate blocked), B writes the
    same file under a different spelling. B must wait; both fields survive."""
    from agentb.fsutil import update_record
    d = tmp_path / "memory"; d.mkdir()
    (tmp_path / "sub").mkdir()
    path_a = d / "m1.json"
    path_b = tmp_path / "sub" / ".." / "memory" / "m1.json"
    path_a.write_text(json.dumps({"id": "m1"}), encoding="utf-8")
    inside, release = threading.Event(), threading.Event()

    def mutate_a(rec):
        inside.set()
        release.wait(5)
        rec["a"] = True
        return rec

    ta = threading.Thread(target=update_record, args=(path_a, mutate_a))
    ta.start()
    assert inside.wait(5)
    tb = threading.Thread(target=update_record,
                          args=(path_b, lambda rec: {**rec, "b": True}))
    tb.start()
    tb.join(0.3)
    assert tb.is_alive(), "B wrote while A held the record"
    release.set()
    ta.join(5); tb.join(5)
    assert json.loads(path_a.read_text(encoding="utf-8")) == {"id": "m1", "a": True, "b": True}


def test_update_record_contract(tmp_path):
    from agentb.fsutil import update_record
    p = tmp_path / "m.json"
    assert update_record(p, lambda old: None) == (None, None)       # missing: nothing written
    assert not p.exists()
    p.write_text(json.dumps({"id": "m"}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        update_record(p, lambda old: (_ for _ in ()).throw(RuntimeError("no")))
    assert json.loads(p.read_text()) == {"id": "m"}                  # a raising mutate writes nothing
    assert update_record(p, lambda old: {**old, "x": 1})[1] == {"id": "m", "x": 1}  # lock released
    p.write_text("{torn", encoding="utf-8")
    seen = []
    with pytest.raises(ValueError):
        update_record(p, lambda old: seen.append(old))
    assert seen == [], "an unreadable record must raise before mutate runs"


def test_a_reclassify_cannot_clobber_a_stamp_written_between_its_read_and_write(tmp_path, monkeypatch):
    """W7 vs a demote written from a worker. The demote starts right after
    reclassify's re-read and is given time to land; with the lock it waits,
    then lands after the write, so the stamp and the new category both stay."""
    from agentb import fsutil
    from agentb.classify import reclassify_memory_dir
    d = tmp_path / "memory"; d.mkdir()
    path = d / "m1.json"
    path.write_text(json.dumps({"id": "m1", "summary": "IGOR runs nightly", "key_facts": [],
                                "category": "unknown"}), encoding="utf-8")
    real_read = Path.read_text
    reads, demoter = [], []

    def demote(rec):
        rec["superseded_by"] = "demoted:test:1"
        return rec

    def read_text(self, *a, **k):
        out = real_read(self, *a, **k)
        if self == path:
            reads.append(1)
            if len(reads) == 2:            # the re-read before the write
                t = threading.Thread(target=fsutil.update_record, args=(path, demote))
                t.start(); demoter.append(t)
                t.join(0.5)                 # unlocked code: the stamp lands right here
        return out

    monkeypatch.setattr(Path, "read_text", read_text)

    class Reasoner:
        async def generate(self, *a, **k):
            return "topology"

    asyncio.run(reclassify_memory_dir(d, Reasoner(), use_breaker=False))
    demoter[0].join(5)
    monkeypatch.setattr(Path, "read_text", real_read)
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["category"] == "topology"
    assert rec["superseded_by"] == "demoted:test:1", "the reclassify write erased the demote"


# ── W1: writeback ──

def test_a_demote_between_the_writeback_write_and_upsert_leaves_no_vector(client, tmp_path, monkeypatch):  # noqa: F811
    import agentb.server as srv
    real_get = srv.get_ledger
    fired = []

    class Ledger:
        def __init__(self, inner):
            self.inner = inner

        def seal(self, entry, *a, **k):
            out = self.inner.seal(entry, *a, **k)
            if not fired and entry.get("summary") == "the racing memory":
                fired.append(entry["id"])
                # A real demote request, served by the loop while the
                # writeback awaits this seal in its worker.
                r = client.post("/memories/demote", json={
                    "agent_id": "cc", "memory_id": entry["id"], "reason": "wrong", "by": "guy"})
                assert r.status_code == 200 and r.json()["demoted"] is True, r.text
            return out

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(srv, "get_ledger", lambda d: Ledger(real_get(d)))
    mid = _save(client, "cc", "the racing memory")
    assert fired == [mid]
    rec = json.loads((tmp_path / "agents" / "cc" / "memory" / f"{mid}.json").read_text())
    assert rec["superseded_by"].startswith("demoted:guy:")
    vec = VecStore(tmp_path / "agents" / "cc" / "vec_index.sqlite")
    try:
        assert not vec.has(mid), "the writeback put a demoted memory's vector back"
    finally:
        vec.close()


def test_superseding_a_demoted_memory_keeps_the_demote(client, tmp_path):  # noqa: F811
    """R2: the first stamp stays; the vector stays out."""
    old = _save(client, "cc", "the first version")
    r = client.post("/memories/demote", json={"agent_id": "cc", "memory_id": old,
                                               "reason": "wrong", "by": "guy"})
    assert r.json()["demoted"] is True
    _save(client, "cc", "the second version", supersedes=[old])
    rec = json.loads((tmp_path / "agents" / "cc" / "memory" / f"{old}.json").read_text())
    assert rec["superseded_by"].startswith("demoted:guy:")
    assert rec["demote_reason"] == "wrong"
    vec = VecStore(tmp_path / "agents" / "cc" / "vec_index.sqlite")
    try:
        assert not vec.has(old)
    finally:
        vec.close()


# ── W2: Analyst / Muse note ──

def test_a_demote_between_the_note_write_and_upsert_leaves_no_vector(tmp_path, monkeypatch):
    import agentb.analyst as analyst_mod
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir, "log1", "CC: we chose Hetzner over artforge self-host for blast radius.")
    store = VecStore(tmp_path / "vec.sqlite")
    real_get = analyst_mod.get_ledger
    fired, holder = [], {}

    class Ledger:
        def __init__(self, inner):
            self.inner = inner

        def seal(self, entry, *a, **k):
            out = self.inner.seal(entry, *a, **k)
            if not fired:
                fired.append(entry["id"])
                _stamp(memory_dir / f"{entry['id']}.json")            # the demote's write
                holder["loop"].call_soon_threadsafe(store.delete, entry["id"])  # its delete, on the loop
            return out

    monkeypatch.setattr(analyst_mod, "get_ledger", lambda d: Ledger(real_get(d)))

    async def run():
        holder["loop"] = asyncio.get_running_loop()
        return await analyze_tenant("cc", memory_dir, store, ScriptedReasoner(reply=NOTE_REPLY),
                                    ScriptedEmbedder(VEC_A), config=AnalysisConfig())

    asyncio.run(run())
    assert len(fired) == 1
    assert json.loads((memory_dir / f"{fired[0]}.json").read_text())["superseded_by"]
    assert not store.has(fired[0]), "the analyst put a demoted note's vector back"
    store.close()


# ── W3: archived session ──

def _archive_once(app, tenant, session_id, summary):
    sm = tenant["sessions"]

    async def fake_archive(_summarize):
        return [{"session_id": session_id, "summary": summary,
                 "last_exchange": "2026-11-18T10:00:00+00:00", "key_facts": []}]

    real = sm.archive_hot_sessions
    sm.archive_hot_sessions = fake_archive
    try:
        asyncio.run(app.state.maintenance_cycle(1))
    finally:
        sm.archive_hot_sessions = real


def _archived_mid(tenant):
    mids = [json.loads(p.read_text(encoding="utf-8"))["id"]
            for p in tenant["memory_dir"].glob("*.json")
            if "archived-session" in json.loads(p.read_text(encoding="utf-8")).get("additional_tags", [])]
    assert len(mids) == 1, mids
    return mids[0]


def test_a_demoted_archived_session_is_not_rewritten_resealed_or_reindexed(tmp_path):
    """(a)"""
    app = make_app(tmp_path, reasoner=VerdictReasoning(reply="x"))
    with TestClient(app) as c:
        c.post("/ingest", json={"agent_id": "cc", "prompt": "hi", "response": "yo"},
               headers=_auth(MASTER))
        tenant = app.state.tenants._tenants["cc"]
        _archive_once(app, tenant, "sess-1", "cc talked about anvils")
        mid = _archived_mid(tenant)
        assert tenant["vec"].has(mid)
        r = c.post("/memories/demote", json={"agent_id": "cc", "memory_id": mid,
                                              "reason": "wrong", "by": "guy"}, headers=_auth(MASTER))
        assert r.json()["demoted"] is True
        ledger = tenant["memory_dir"].parent / "ledger.jsonl"
        seals = ledger.read_text(encoding="utf-8").count(mid)
        _archive_once(app, tenant, "sess-1", "cc talked about anvils AGAIN, other words")
        rec = json.loads((tenant["memory_dir"] / f"{mid}.json").read_text(encoding="utf-8"))
        assert rec["superseded_by"].startswith("demoted:guy:")
        assert rec["summary"] == "cc talked about anvils"
        assert ledger.read_text(encoding="utf-8").count(mid) == seals, "re-sealed"
        assert not tenant["vec"].has(mid), "re-indexed"


def test_a_demote_during_the_archival_embed_leaves_no_vector(tmp_path):
    """(b) for W3: the demote lands while the archival writer awaits its embed."""
    holder = {}

    class DemotingEmbedding(FakeEmbedding):
        async def embed(self, text, *, use_breaker=True, task_type="document"):
            if text == "a session that gets demoted" and "done" not in holder:
                holder["done"] = True
                mid = _archived_mid(holder["tenant"])
                r = holder["client"].post("/memories/demote", json={
                    "agent_id": "cc", "memory_id": mid, "reason": "wrong", "by": "guy"},
                    headers=_auth(MASTER))
                assert r.json()["demoted"] is True, r.text
            return await super().embed(text, use_breaker=use_breaker, task_type=task_type)

    app = make_app(tmp_path, reasoner=VerdictReasoning(reply="x"), embedder=DemotingEmbedding())
    with TestClient(app) as c:
        c.post("/ingest", json={"agent_id": "cc", "prompt": "hi", "response": "yo"},
               headers=_auth(MASTER))
        tenant = app.state.tenants._tenants["cc"]
        holder.update(client=c, tenant=tenant)
        _archive_once(app, tenant, "sess-2", "a session that gets demoted")
        assert holder.get("done")
        mid = _archived_mid(tenant)
        rec = json.loads((tenant["memory_dir"] / f"{mid}.json").read_text(encoding="utf-8"))
        assert rec["superseded_by"].startswith("demoted:guy:")
        assert not tenant["vec"].has(mid), "archival put a demoted memory's vector back"
        assert mid not in tenant["pending"].ids()


# ── W8: the proposal applier's undo ──

def test_a_failed_close_undoes_only_the_applier_fields_and_keeps_a_demote(tmp_path, monkeypatch):
    """R3: undo() restores category / classified_by / category_original /
    needs_reclassification (absent ones removed again), and a demote that
    landed between apply and undo stays."""
    from agentb.facts_store import FactsStore
    from unittest.mock import patch
    from agentb.config import (
        AgentBConfig, CacheConfig, ClassificationConfig, DEFAULT_PERSONAS,
        ProviderConfig, ResilientProviderConfig, ServerConfig,
    )
    from tests.test_context_authority import FakeEmbedding as CAEmbedding, FakeReasoning
    from tests.test_proposal_envelope import _FAIL_CLOSE, _post_proposal, _writeback
    real_resolve = FactsStore.resolve_proposal

    def resolve(self, pid, action, by, reason="", apply_memory=None):
        def apply(*a):
            out = apply_memory(*a)
            # a demote's record half, landing after apply, before the close fails
            path = next(Path(tmp_path).rglob(f"{a[1]}.json"))
            _stamp(path)
            return out
        return real_resolve(self, pid, action, by, reason,
                            apply if apply_memory is not None else None)

    monkeypatch.setattr(FactsStore, "resolve_proposal", resolve)
    monkeypatch.delenv("MNEMO_JEV_KEY_FILE", raising=False)
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
        data_dir=str(tmp_path), classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS))
    with patch("agentb.server.create_resilient_embedding", return_value=CAEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        app_client = TestClient(create_app(cfg), raise_server_exceptions=False)
    with app_client as c:
        mid = _writeback(c)
        path = next(Path(tmp_path).rglob(f"{mid}.json"))
        before = json.loads(path.read_text(encoding="utf-8"))
        rec = dict(before, needs_reclassification=True)
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        pid = _post_proposal(c, mid).json()["proposal_id"]
        conn = sqlite3.connect(tmp_path / "facts.sqlite")
        conn.execute(_FAIL_CLOSE); conn.commit(); conn.close()
        r = c.post(f"/proposals/{pid}/resolve", json={"action": "accept", "by": "cc", "reason": "Guy"})
        assert r.status_code == 500
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["superseded_by"].startswith("demoted:test:"), "undo erased the demote"
        assert after["category"] == before["category"]
        assert after.get("classified_by") == before.get("classified_by")
        assert "category_original" not in after                       # added by apply: removed
        assert after["needs_reclassification"] is True                 # popped by apply: back
