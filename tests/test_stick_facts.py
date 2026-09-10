"""Cortex Stick facts channel — the ladder-respecting row merge.

The contract under test:
  - facts travel between hosts through the stick (encrypted like everything)
  - same value both sides → reassert-merge (max confidence)
  - the promotion ladder holds across machines: verified survives a NEWER
    high_probability contradiction
  - a NEWER demotion (confidence='false') propagates; a STALE one loses to a
    later re-establishment
  - every courier-applied change leaves a fact_history audit row
  - merge is idempotent and never version-churns a settled stick
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from agentb.facts_store import FactsStore
from agentb.stick import init_stick, sync, unlock_stick
from agentb.stick_facts import FACTS_REL, merge_row

PASS = "facts courier test"
FAST_KDF = {"name": "scrypt", "n": 1 << 12, "r": 8, "p": 1}


@pytest.fixture
def world(tmp_path):
    """Two hosts + an ENCRYPTED stick (facts must be covered by custody)."""
    a, b, mount = tmp_path / "host_a", tmp_path / "host_b", tmp_path / "usb"
    a.mkdir(); b.mkdir(); mount.mkdir()
    stick = init_stick(mount, passphrase=PASS, kdf_params=FAST_KDF)
    unlock_stick(stick, a, PASS)
    unlock_stick(stick, b, PASS)
    return a, b, stick


def courier(host: Path, stick: Path, hid: str, **kw):
    return sync(host, stick, host_id=hid, pad=False, **kw)


def store(host: Path) -> FactsStore:
    return FactsStore(host / "facts.sqlite")


def set_last_updated(host: Path, entity: str, attribute: str, ts: float) -> None:
    conn = sqlite3.connect(str(host / "facts.sqlite"))
    conn.execute("UPDATE facts SET last_updated=? WHERE entity=? AND attribute=?",
                 (ts, entity, attribute))
    conn.commit(); conn.close()


def test_fact_travels_and_is_ciphertext(world):
    a, b, stick = world
    store(a).save(entity="host-b", attribute="tailscale_ip",
                  value="100.64.0.1", confidence="verified",
                  evidence_source="test")
    r = courier(a, stick, "host-a")
    assert r.facts_to_stick == 1
    raw = (stick / FACTS_REL).read_bytes()
    assert raw.startswith(b"CSTK\x01") and b"100.64.0.1" not in raw
    r = courier(b, stick, "host-b")
    assert r.facts_to_host == 1
    fact = store(b).get("host-b", "tailscale_ip")
    assert fact is not None and fact.value == "100.64.0.1"
    assert fact.confidence == "verified"


def test_verified_survives_newer_high_probability(world):
    """The ladder's core promise must hold ACROSS machines."""
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="right",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    time.sleep(0.02)
    store(b).save(entity="e", attribute="k", value="wrong-but-newer",
                  confidence="high_probability", evidence_source="test")
    courier(b, stick, "host-b")      # B learns the verified value instead
    fb = store(b).get("e", "k")
    assert fb.value == "right" and fb.confidence == "verified"
    courier(a, stick, "host-a")
    assert store(a).get("e", "k").value == "right"


def test_newer_demotion_propagates(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v1",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    time.sleep(0.02)
    store(b).demote("e", "k", reason="turned out wrong", changed_by="tester")
    courier(b, stick, "host-b")
    courier(a, stick, "host-a")
    assert store(a).get("e", "k") is None            # false is hidden
    hidden = store(a).get("e", "k", include_false=True)
    assert hidden is not None and hidden.confidence == "false"


def test_stale_demotion_loses_to_reestablishment(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v1",
                  confidence="high_probability", evidence_source="test")
    store(a).demote("e", "k", reason="doubt", changed_by="tester")
    courier(a, stick, "host-a")
    time.sleep(0.02)
    store(b).save(entity="e", attribute="k", value="v2-reborn",
                  confidence="high_probability", evidence_source="test")
    courier(b, stick, "host-b")      # newer re-establishment wins
    assert store(b).get("e", "k").value == "v2-reborn"
    courier(a, stick, "host-a")
    assert store(a).get("e", "k").value == "v2-reborn"


def test_same_value_reassert_merges_confidence(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="same",
                  confidence="high_probability", evidence_source="a")
    store(b).save(entity="e", attribute="k", value="same",
                  confidence="verified", evidence_source="b")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    courier(a, stick, "host-a")
    fa, fb = store(a).get("e", "k"), store(b).get("e", "k")
    assert fa.confidence == fb.confidence == "verified"
    assert fa.value == "same"


def test_equal_rank_newer_wins_and_converges(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="old",
                  confidence="high_probability", evidence_source="a")
    store(b).save(entity="e", attribute="k", value="new",
                  confidence="high_probability", evidence_source="b")
    set_last_updated(a, "e", "k", 1000.0)
    set_last_updated(b, "e", "k", 2000.0)
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    courier(a, stick, "host-a")
    assert store(a).get("e", "k").value == "new"
    assert store(b).get("e", "k").value == "new"


def test_courier_import_writes_audit_history(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v1",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    conn = sqlite3.connect(str(b / "facts.sqlite"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM fact_history WHERE entity='e'").fetchall()
    conn.close()
    assert any(r["reason"] == "cortex stick courier merge"
               and str(r["changed_by"]).startswith("stick:") for r in rows)


def test_idempotent_and_no_version_churn(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    manifest = json.loads((stick / "manifest.json").read_text())
    ver_before = manifest["files"][FACTS_REL]["version"]
    for hid, host in (("host-a", a), ("host-b", b)):
        r = courier(host, stick, hid)
        assert not r.changed, f"{hid} facts re-sync was not a no-op"
    manifest = json.loads((stick / "manifest.json").read_text())
    assert manifest["files"][FACTS_REL]["version"] == ver_before


def test_no_facts_anywhere_is_silent(world):
    a, _, stick = world
    r = courier(a, stick, "host-a")
    assert r.facts_to_host == 0 and r.facts_to_stick == 0
    assert not (stick / FACTS_REL).exists()


def test_dry_run_touches_nothing(world):
    a, _, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    r = courier(a, stick, "host-a", dry_run=True)
    assert r.facts_to_stick == 1
    assert not (stick / FACTS_REL).exists()


def test_facts_false_in_config_skips(world):
    a, _, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    r = courier(a, stick, "host-a", facts=False)
    assert r.facts_to_stick == 0
    assert not (stick / FACTS_REL).exists()


def test_tampered_facts_file_refuses(world):
    """facts.jsonl is manifest-covered — a flipped byte torn-gates the sync,
    and even a laundered repair can't decrypt it."""
    from agentb.stick import StickError, repair_manifest
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    p = stick / FACTS_REL
    raw = bytearray(p.read_bytes())
    raw[-1] ^= 0xFF
    p.write_bytes(bytes(raw))
    with pytest.raises(StickError, match="TORN GENERATION"):
        courier(b, stick, "host-b")
    repair_manifest(stick)
    with pytest.raises(StickError, match="DECRYPT FAILED"):
        courier(b, stick, "host-b")


def test_merge_row_is_commutative():
    now = time.time()
    r1 = {"entity": "e", "attribute": "k", "value": "x",
          "confidence": "verified", "evidence_source": "a",
          "source_memory_id": None, "source_agent": None,
          "created_at": now - 10, "last_updated": now - 5}
    r2 = {**r1, "value": "y", "confidence": "high_probability",
          "last_updated": now}
    assert merge_row(r1, r2) == merge_row(r2, r1)
    r3 = {**r1, "value": "z", "confidence": "false", "last_updated": now + 1}
    assert merge_row(r1, r3) == merge_row(r3, r1)
    # demote() keeps the value — a SAME-value newer 'false' must still win
    r4 = {**r1, "confidence": "false", "last_updated": now + 1}
    assert merge_row(r1, r4) == merge_row(r4, r1)
    assert merge_row(r1, r4)["confidence"] == "false"
    assert merge_row(r1, None)["value"] == "x"
    # equal value + equal last_updated + differing aux fields — the one spot
    # where first-arg-wins would silently break commutativity (sha ping-pong)
    r5 = {**r1, "evidence_source": "b", "source_agent": "other"}
    assert merge_row(r1, r5) == merge_row(r5, r1)
    # a synthesized reassert row is a fixed point: re-merging against either
    # input (or itself) reproduces it exactly
    r6 = {**r1, "confidence": "high_probability", "created_at": now - 20,
          "last_updated": now - 2}
    merged = merge_row(r1, r6)
    assert merge_row(merged, r1) == merged
    assert merge_row(merged, r6) == merged
    assert merge_row(merged, dict(merged)) == merged


def test_courier_never_clobbers_concurrent_live_write(world):
    """TOCTOU guard: a fact the live server writes BETWEEN the courier's
    snapshot and its apply transaction must win its merge, not be
    overwritten by the stale snapshot winner."""
    from agentb.stick_facts import apply_to_host, dump_facts

    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="stale",
                  confidence="high_probability", evidence_source="a")
    courier(a, stick, "host-a")

    # B takes its snapshot (empty for key e/k is NOT the case — B has synced)
    courier(b, stick, "host-b")
    snapshot = {(r["entity"], r["attribute"]): r for r in dump_facts(b / "facts.sqlite")}
    time.sleep(0.02)
    # ...the live server verifies a NEWER value after the snapshot...
    store(b).save(entity="e", attribute="k", value="live-verified",
                  confidence="verified", evidence_source="server")
    # ...and the courier applies winners computed from the stale snapshot.
    stale_winner = {**snapshot[("e", "k")], "value": "from-stick",
                    "confidence": "high_probability",
                    "last_updated": snapshot[("e", "k")]["last_updated"] + 0.001}
    applied = apply_to_host(b / "facts.sqlite", [stale_winner], snapshot, "test")
    assert applied == 0
    fact = store(b).get("e", "k")
    assert fact.value == "live-verified" and fact.confidence == "verified"


# ── 4.21.2: fact_history travels (the S04 offline-carry finding) ──

from agentb.stick_facts import HISTORY_REL, history_row_id  # noqa: E402


def history_of(host: Path, entity: str, attribute: str) -> list[dict]:
    return store(host).history(entity, attribute)


def test_receiving_host_can_answer_what_it_used_to_be(world):
    """S04 finding: B answered '40w' but not 'what did it used to be'."""
    a, b, stick = world
    s = store(a)
    s.save(entity="lamp", attribute="power", value="30w",
           confidence="verified", evidence_source="test", source_agent="cc")
    s.save(entity="lamp", attribute="power", value="40w",
           confidence="verified", evidence_source="test", source_agent="cc")
    assert [h["new_value"] for h in history_of(a, "lamp", "power")] == ["40w", "30w"]
    courier(a, stick, "host-a")
    r = courier(b, stick, "host-b")
    assert r.history_to_host == 2
    origin = [h for h in history_of(b, "lamp", "power")
              if not str(h["changed_by"] or "").startswith("stick:")]
    assert [h["new_value"] for h in origin] == ["40w", "30w"]
    assert origin[0]["old_value"] == "30w"          # the fact's past is now on B
    assert store(b).get("lamp", "power").value == "40w"


def test_history_merge_is_idempotent_and_courier_rows_stay_local(world):
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    manifest = json.loads((stick / "manifest.json").read_text())
    ver_before = manifest["files"][HISTORY_REL]["version"]
    for hid, host in (("host-b", b), ("host-a", a)):
        r = courier(host, stick, hid)
        assert not r.changed, f"{hid} re-sync was not a no-op"
        assert (r.history_to_host, r.history_to_stick) == (0, 0)
    manifest = json.loads((stick / "manifest.json").read_text())
    assert manifest["files"][HISTORY_REL]["version"] == ver_before
    # B's courier audit row never crossed back to A
    assert not any(str(h["changed_by"] or "").startswith("stick:")
                   for h in history_of(a, "e", "k"))
    # and A's origin row exists exactly once on B
    ids = [history_row_id(h) for h in history_of(b, "e", "k")
           if not str(h["changed_by"] or "").startswith("stick:")]
    assert len(ids) == len(set(ids)) == 1


def test_history_is_ciphertext_and_manifest_covered(world):
    from agentb.stick import StickError
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="secret-value",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    p = stick / HISTORY_REL
    assert p.is_file() and b"secret-value" not in p.read_bytes()
    raw = bytearray(p.read_bytes())
    raw[-1] ^= 0xFF
    p.write_bytes(bytes(raw))
    with pytest.raises(StickError, match="TORN GENERATION"):
        courier(b, stick, "host-b")


def test_demotion_history_travels_both_ways(world):
    """Two hosts each contribute origin rows; both end with the union."""
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    store(b).demote("e", "k", reason="proven wrong", changed_by="opie")
    courier(b, stick, "host-b")
    r = courier(a, stick, "host-a")
    assert r.history_to_host == 1
    reasons = {h["reason"] for h in history_of(a, "e", "k")}
    assert "demote: proven wrong" in reasons


def test_locked_history_table_aborts_instead_of_duplicating(world, monkeypatch):
    """Reviewer finding: a swallowed OperationalError read as 'no history'
    and re-imported the whole stick log. A real error must propagate."""
    import agentb.stick_facts as sf
    a, b, stick = world
    store(a).save(entity="e", attribute="k", value="v",
                  confidence="verified", evidence_source="test")
    courier(a, stick, "host-a")
    courier(b, stick, "host-b")
    real_connect = sqlite3.connect

    class LockedConn(sqlite3.Connection):
        def execute(self, sql, *p):
            if "FROM fact_history" in sql:
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *p)

    def locked_connect(path, *args, **kw):
        if str(path).endswith("facts.sqlite"):
            kw["factory"] = LockedConn
        return real_connect(path, *args, **kw)

    monkeypatch.setattr(sf.sqlite3, "connect", locked_connect)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        courier(b, stick, "host-b")
    monkeypatch.undo()
    r = courier(b, stick, "host-b")
    assert (r.history_to_host, r.history_to_stick) == (0, 0)
    ids = [history_row_id(h) for h in history_of(b, "e", "k")
           if not str(h["changed_by"] or "").startswith("stick:")]
    assert len(ids) == len(set(ids)) == 1


def test_pre_history_facts_db_is_just_empty_history(world):
    a, b, stick = world
    conn = sqlite3.connect(str(a / "facts.sqlite"))
    conn.execute("CREATE TABLE facts (entity TEXT, attribute TEXT, value TEXT, "
                 "confidence TEXT, evidence_source TEXT, source_memory_id TEXT, "
                 "source_agent TEXT, created_at REAL, last_updated REAL, "
                 "PRIMARY KEY (entity, attribute))")
    conn.commit(); conn.close()
    from agentb.stick_facts import dump_history
    assert dump_history(a / "facts.sqlite") == []
