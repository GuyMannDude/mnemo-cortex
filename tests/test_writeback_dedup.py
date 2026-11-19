"""Write-time near-duplicate contract: bounded, explainable, lane-local."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, AgentConfig, CacheConfig, ClassificationConfig,
    DEFAULT_PERSONAS, ProviderConfig, ResilientProviderConfig, ServerConfig,
)
from agentb.rulekeeper import format_advisory, scan_tenant
from agentb.vec import VecStore

VEC = [0.0] * 768
VEC[0] = 1.0
STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
          "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    status = STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    status = STATUS
    async def generate(self, *args, **kwargs): return "doctrine"
    async def health_check(self): return True


@pytest.fixture
def world(tmp_path):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama")),
        cache=CacheConfig(), classification=ClassificationConfig(enabled=False),
        server=ServerConfig(host="127.0.0.1", port=50098),
        data_dir=str(tmp_path / "default"), personas=dict(DEFAULT_PERSONAS),
        agents={
            "cody": AgentConfig(data_dir=str(tmp_path / "cody")),
            "cc": AgentConfig(data_dir=str(tmp_path / "cc")),
        },
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as client:
            yield client, tmp_path


TEXT = "Bus receipts to Guy must be the final line with recipient and ping number"


def save(client, session, *, agent="cody", text=TEXT, **extra):
    body = {"session_id": session, "summary": text, "agent_id": agent,
            "category": "doctrine", "source": "inferred", **extra}
    return client.post("/writeback", json=body)


def test_interactive_duplicate_is_held_with_scores(world):
    client, _ = world
    first = save(client, "first").json()
    held = save(client, "second").json()
    assert first["status"] == "archived"
    assert held["status"] == "held"
    assert held["memory_id"] == ""
    assert held["near_duplicates"][0]["id"] == first["memory_id"]
    assert held["near_duplicates"][0]["cosine"] >= 0.80
    assert held["near_duplicates"][0]["overlap"] >= 0.55
    assert "receipt" in held["near_duplicates"][0]["shared_tokens"]


def test_force_inserts_and_records_near_dup(world):
    client, root = world
    first = save(client, "first").json()
    forced = save(client, "forced", force=True).json()
    assert forced["status"] == "archived"
    stored = json.loads((root / "cody" / "memory" / f"{forced['memory_id']}.json").read_text())
    assert stored["near_dup_of"] == [first["memory_id"]]


def test_supersede_keeps_history_but_demotes_old(world):
    client, root = world
    first = save(client, "first").json()
    newer = save(client, "newer", supersedes=[first["memory_id"]]).json()
    old_path = root / "cody" / "memory" / f"{first['memory_id']}.json"
    assert old_path.exists()
    assert json.loads(old_path.read_text())["superseded_by"] == newer["memory_id"]
    recalled = client.post("/context", json={"prompt": TEXT, "agent_id": "cody",
                                               "exclude_categories": []}).json()
    assert first["memory_id"] not in {c.get("memory_id") for c in recalled["chunks"]}


def test_auto_capture_never_blocks(world):
    client, root = world
    first = save(client, "first").json()
    auto = save(client, "auto", category="session_log").json()
    assert auto["status"] == "archived"
    stored = json.loads((root / "cody" / "memory" / f"{auto['memory_id']}.json").read_text())
    assert stored["near_dup_of"] == [first["memory_id"]]


def test_short_text_never_holds(world):
    client, _ = world
    save(client, "short-one", text="alpha beta gamma")
    assert save(client, "short-two", text="alpha beta gamma").json()["status"] == "archived"


def test_cross_lane_isolation(world):
    client, _ = world
    save(client, "cc-first", agent="cc")
    assert save(client, "cody-first", agent="cody").json()["status"] == "archived"


def test_allowlist_pair_proceeds_clean(world):
    client, root = world
    first = save(client, "first").json()
    (root / "cody" / "dedup-allow.txt").write_text(
        f"allowed|{first['memory_id']}\n", encoding="utf-8")
    allowed = save(client, "allowed").json()
    assert allowed["status"] == "archived"
    assert allowed["near_duplicates"] == []


def test_rulekeeper_blind_store_is_loud(tmp_path):
    store = VecStore(tmp_path / "vec_index.sqlite")
    try:
        result = scan_tenant(tmp_path / "memory", store)
    finally:
        store.close()
    assert result["exit_code"] == 2
    assert "BLIND STORE" in format_advisory("cody", result)


def test_rulekeeper_reports_without_mutating(world):
    client, root = world
    first = save(client, "first").json()
    forced = save(client, "forced", force=True).json()
    memory_dir = root / "cody" / "memory"
    before = {p.name: p.read_bytes() for p in memory_dir.glob("*.json")}
    store = VecStore(root / "cody" / "vec_index.sqlite")
    try:
        result = scan_tenant(memory_dir, store)
    finally:
        store.close()
    after = {p.name: p.read_bytes() for p in memory_dir.glob("*.json")}
    assert result["exit_code"] == 1
    assert result["pairs"][0]["new_id"] in {first["memory_id"], forced["memory_id"]}
    assert before == after
    assert "nothing merged or demoted" in format_advisory("cody", result)


def test_forced_near_duplicate_outranks_the_memory_it_revises(world):
    # E1 (proving ground S02, 2026-09-05). Same embedding for both (FakeEmbedding),
    # same age; the FIRST memory earns access credit from a recall, which at
    # the shipped weights would keep it on top. The forced write carries
    # near_dup_of = [first]; at recall the reviser must serve first, and the
    # revised memory must still be served (demoted, never dropped).
    client, _ = world
    first = save(client, "first", category="topology", text="the nozzle is clogged and printing badly").json()
    client.post("/context", json={"prompt": "nozzle", "agent_id": "cody", "exclude_categories": []})
    forced = save(client, "second", category="topology", force=True,
                  text="the nozzle is clogged? no — it was replaced and prints cleanly").json()
    assert forced["status"] == "archived"
    chunks = client.post("/context", json={"prompt": "nozzle", "agent_id": "cody",
                                           "exclude_categories": []}).json()["chunks"]
    ids = [c["memory_id"] for c in chunks]
    assert ids.index(forced["memory_id"]) < ids.index(first["memory_id"])


def test_batch_write_past_the_gate_does_not_acquire_revision_power(world):
    # review 2026-09-05: near_dup_of was stamped on non-interactive writes too
    # (batch / routine logs skip the hold), so a bulk import could re-order
    # other callers' served windows. Only a FORCED write is a revision.
    client, root = world
    first = save(client, "first", category="topology", text="the nozzle is clogged and printing badly").json()
    client.post("/context", json={"prompt": "nozzle", "agent_id": "cody", "exclude_categories": []})
    batch = save(client, "second", category="topology", batch=True,
                 text="the nozzle is clogged? no — it was replaced and prints cleanly").json()
    assert batch["status"] == "archived"
    stored = json.loads((root / "cody" / "memory" / f"{batch['memory_id']}.json").read_text())
    assert stored["near_dup_of"] == [first["memory_id"]]            # audit link kept
    assert "near_dup_forced" not in stored                          # but no revision claim
    chunks = client.post("/context", json={"prompt": "nozzle", "agent_id": "cody",
                                           "exclude_categories": []}).json()["chunks"]
    ids = [c["memory_id"] for c in chunks]
    assert ids.index(first["memory_id"]) < ids.index(batch["memory_id"])


# ── a demoted memory never holds a save (follow-up to 9d) ──

def test_a_demoted_memory_back_in_the_index_does_not_hold_a_save(world):
    client, root = world
    first = save(client, "first").json()
    # Demote on disk, leave the vector in the index: what an older backfill left.
    path = root / "cody" / "memory" / f"{first['memory_id']}.json"
    rec = json.loads(path.read_text())
    rec["superseded_by"] = "demoted:cc:1"
    path.write_text(json.dumps(rec))
    assert client.app.state.tenants.get("cody")["vec"].has(first["memory_id"])
    second = save(client, "second").json()
    assert second["status"] == "archived"
    stored = json.loads((root / "cody" / "memory" / f"{second['memory_id']}.json").read_text())
    assert first["memory_id"] not in (stored.get("near_dup_of") or [])


def test_a_live_twin_ranked_behind_a_demoted_one_is_still_found(tmp_path):
    from agentb.dedup import _search
    store = VecStore(tmp_path / "vec.sqlite")
    near = [0.0] * 768
    near[0], near[1] = 0.99, 0.141          # cosine ~0.99 to VEC, ranks second
    for mid, vec, demoted in (("dead", VEC, True), ("live", near, False)):
        rec = tmp_path / f"{mid}.json"
        rec.write_text(json.dumps({"id": mid, "summary": TEXT,
                                   **({"superseded_by": "x"} if demoted else {})}))
        store.upsert(mid, TEXT, list(vec), source_file=str(rec), created_at=1.0)
    store.close()
    found = _search(tmp_path / "vec.sqlite", list(VEC), TEXT, "new", tmp_path / "no-allow.txt",
                    top_k=1, cosine_threshold=0.80, overlap_threshold=0.55)
    assert [m["id"] for m in found] == ["live"]


def test_a_forced_save_never_records_a_demoted_memory_as_its_near_dup(world):
    client, root = world
    first = save(client, "first").json()
    path = root / "cody" / "memory" / f"{first['memory_id']}.json"
    rec = json.loads(path.read_text())
    rec["superseded_by"] = "demoted:cc:1"
    path.write_text(json.dumps(rec))
    forced = save(client, "second", force=True).json()
    assert forced["status"] == "archived"
    stored = json.loads((root / "cody" / "memory" / f"{forced['memory_id']}.json").read_text())
    assert first["memory_id"] not in (stored.get("near_dup_of") or [])


def test_the_check_still_looks_at_no_more_than_top_k_live_hits(tmp_path):
    from agentb.dedup import _search
    store = VecStore(tmp_path / "vec.sqlite")
    for mid in ("one", "two"):
        rec = tmp_path / f"{mid}.json"
        rec.write_text(json.dumps({"id": mid, "summary": TEXT}))
        store.upsert(mid, TEXT, list(VEC), source_file=str(rec), created_at=1.0)
    store.close()
    found = _search(tmp_path / "vec.sqlite", list(VEC), TEXT, "new", tmp_path / "no-allow.txt",
                    top_k=1, cosine_threshold=0.80, overlap_threshold=0.55)
    assert len(found) == 1
