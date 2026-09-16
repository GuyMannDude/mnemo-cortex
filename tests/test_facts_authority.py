"""v4.23 authority tiers — the store half.

A tier says WHO may change a slot. `open` keeps the confidence ladder as the
only rule; `probe` answers to a machine (`probe:`/`tool:` evidence);
`declared` answers to Guy (`statement:guy…`). Any other write to a locked
slot is recorded as a proposal, never as the value. Specimen: an agent's
loose "Tailscale is the only path" sentence, 2026-09-15.
"""
from __future__ import annotations

import sqlite3

import pytest

from agentb.facts_store import FactsStore, AUTHORITY_LEVELS, evidence_allowed


@pytest.fixture
def store(tmp_path):
    return FactsStore(tmp_path / "facts.sqlite")


def _seed_locked(store, tier="probe"):
    store.save("IGOR-2", "access_paths_from_igor", "SSH+RDP over Tailscale AND LAN",
               "verified", "statement:Guy 2026-09-15 + tool:nc probe", source_agent="cc")
    r = store.set_authority("igor-2", "access_paths_from_igor", tier,
                            "statement:guy S337", changed_by="cc",
                            probe_cmd="nc -z 198.51.100.5 22; nc -z 192.0.2.122 22",
                            probe_host="igor")
    assert r.written, r.reason
    return r


def test_evidence_allowed_table():
    assert evidence_allowed("open", "dream:2026-09-15")
    assert evidence_allowed("probe", "tool:nc -z")
    assert evidence_allowed("probe", "probe:dmidecode")
    assert not evidence_allowed("probe", "statement:guy")
    assert evidence_allowed("declared", "statement:Guy direct")   # case-folded
    assert not evidence_allowed("declared", "tool:nc")
    assert not evidence_allowed("declared", "memory:abc")
    assert set(AUTHORITY_LEVELS) == {"open", "probe", "declared"}


def test_open_slot_is_unchanged_behaviour(store):
    store.save("guy", "location", "HMB", "verified", "statement:Guy", source_agent="cc")
    r = store.save("guy", "location", "Somewhere else", "high_probability", "dream:x", source_agent="dreamer")
    assert r.written is False and r.was_contradiction is True
    assert r.proposal_id is None and r.authority == "open"
    assert r.reason.startswith("rejected")
    assert store.proposals() == []


def test_locked_slot_turns_wrong_evidence_into_a_proposal(store):
    _seed_locked(store, "probe")
    # The 09-15 specimen: an agent sentence, inferred, newer, wrong.
    r = store.save("igor-2", "access_paths_from_igor", "Tailscale only", "verified",
                   "memory:d6a6549caf6ed4c2", source_agent="cc")
    assert r.written is False
    assert r.reason.startswith("locked:probe")
    assert r.proposal_id == 1 and r.authority == "probe"
    assert store.get("igor-2", "access_paths_from_igor").value == "SSH+RDP over Tailscale AND LAN"
    rows = store.proposals()
    assert len(rows) == 1
    assert rows[0]["proposed_value"] == "Tailscale only"
    assert rows[0]["current_value"] == "SSH+RDP over Tailscale AND LAN"
    assert rows[0]["status"] == "pending"
    # and it shows in the contradictions debug view
    assert any(c["reason"].startswith("proposal #1") for c in store.contradictions())


def test_locked_slot_same_value_wrong_evidence_is_quiet(store):
    _seed_locked(store, "probe")
    r = store.save("igor-2", "access_paths_from_igor", "SSH+RDP over Tailscale AND LAN",
                   "verified", "file:brain-facts-seed.yaml", source_agent="seed")
    assert r.written is False and r.was_contradiction is False
    assert "value unchanged" in r.reason
    assert store.proposals() == []


def test_locked_slot_accepts_its_own_kind(store):
    _seed_locked(store, "probe")
    r = store.save("igor-2", "access_paths_from_igor", "SSH only (RDP off)", "verified",
                   "tool:nc -z 3389 refused", source_agent="cc")
    assert r.written is True and r.was_contradiction is True
    assert store.get("igor-2", "access_paths_from_igor").value == "SSH only (RDP off)"
    # the ladder still applies inside the tier: high_probability cannot beat verified
    r2 = store.save("igor-2", "access_paths_from_igor", "nothing open", "high_probability",
                    "tool:flaky", source_agent="cc")
    assert r2.written is False and r2.reason.startswith("rejected")


def test_declared_slot_answers_only_to_guy(store):
    store.save("igor-2", "address_convention", "Tailscale IP by convention", "verified",
               "statement:guy", source_agent="cc")
    assert store.set_authority("igor-2", "address_convention", "declared", "statement:guy").written
    r = store.save("igor-2", "address_convention", "LAN IP", "verified", "tool:ping", source_agent="cc")
    assert r.written is False and r.proposal_id == 1
    r2 = store.save("igor-2", "address_convention", "LAN IP", "verified", "statement:Guy 09-16", source_agent="cc")
    assert r2.written is True


def test_authority_change_needs_guys_word(store):
    store.save("igor", "motherboard", "Dell 0X1Y2Z", "verified", "tool:dmidecode", source_agent="cc")
    r = store.set_authority("igor", "motherboard", "probe", "tool:dmidecode", probe_cmd="dmidecode -t 2")
    assert r.written is False and "statement:guy" in r.reason
    assert store.get("igor", "motherboard").authority == "open"
    r = store.set_authority("igor", "motherboard", "probe", "statement:guy", probe_cmd="dmidecode -t 2", probe_host="igor")
    assert r.written is True
    f = store.get("igor", "motherboard")
    assert f.authority == "probe" and f.probe_cmd == "dmidecode -t 2" and f.probe_host == "igor"
    # unlocking is Guy's too, and clears the probe fields
    assert store.set_authority("igor", "motherboard", "open", "statement:guy").written
    f = store.get("igor", "motherboard")
    assert f.authority == "open" and f.probe_cmd is None
    hist = store.history("igor", "motherboard")
    assert [h["reason"][:22] for h in hist[:2]] == ["authority: probe -> op", "authority: open -> pro"]


def test_probe_tier_needs_a_probe_cmd(store):
    store.save("igor", "bios_version", "1.2.3", "verified", "tool:dmidecode", source_agent="cc")
    r = store.set_authority("igor", "bios_version", "probe", "statement:guy")
    assert r.written is False and "probe_cmd" in r.reason
    with pytest.raises(ValueError):
        store.set_authority("igor", "bios_version", "hard", "statement:guy")
    assert store.set_authority("nope", "x", "declared", "statement:guy").reason == "no such fact"


def test_resolve_proposal_accept_and_reject(store):
    _seed_locked(store, "probe")
    p1 = store.save("igor-2", "access_paths_from_igor", "Tailscale only", "verified",
                    "memory:bad", source_agent="cc").proposal_id
    p2 = store.save("igor-2", "access_paths_from_igor", "SSH+RDP, both IPs, plus VNC", "high_probability",
                    "dream:2026-09-16", source_agent="dreamer").proposal_id
    assert (p1, p2) == (1, 2)

    r = store.resolve_proposal(p1, "reject", by="cc", reason="agent's own loose sentence")
    assert r.written is False and r.reason == "proposal #1 rejected"
    assert store.get("igor-2", "access_paths_from_igor").value == "SSH+RDP over Tailscale AND LAN"
    assert store.resolve_proposal(p1, "accept", by="cc").reason == "already rejected"

    r = store.resolve_proposal(p2, "accept", by="cc", reason="Guy confirmed VNC in chat")
    assert r.written is True and r.was_contradiction is True and r.authority == "probe"
    f = store.get("igor-2", "access_paths_from_igor")
    assert f.value == "SSH+RDP, both IPs, plus VNC"
    assert f.confidence == "verified"
    assert f.evidence_source.startswith("statement:guy accepted proposal #2 via cc <- dream:2026-09-16")
    assert f.authority == "probe"    # accepting does not unlock
    assert store.proposals() == []
    assert {p["status"] for p in store.proposals(status=None)} == {"accepted", "rejected"}
    assert store.resolve_proposal(99, "accept", by="cc").reason == "no such proposal"
    with pytest.raises(ValueError):
        store.resolve_proposal(p2, "maybe", by="cc")
    with pytest.raises(ValueError):
        store.resolve_proposal(p2, "accept", by="")


def test_locked_for_prompt_matches_whole_entity_words(store):
    _seed_locked(store, "probe")
    store.save("igor-20", "role", "does not exist", "verified", "statement:guy", source_agent="cc")
    store.set_authority("igor-20", "role", "declared", "statement:guy")
    store.save("guy", "location", "HMB", "verified", "statement:guy", source_agent="cc")  # open — never pinned

    hits = store.locked_for_prompt("how do I reach IGOR-2 from the laptop?")
    assert [(f.entity, f.attribute) for f in hits] == [("igor-2", "access_paths_from_igor")]
    assert store.locked_for_prompt("where does guy live") == []
    assert store.locked_for_prompt("igor-20 role") and store.locked_for_prompt("igor-20 role")[0].entity == "igor-20"
    assert store.locked_for_prompt("") == []
    # a demoted (false) locked fact does not speak — the demote itself needs
    # the slot's own evidence (review #1); a bare demote would be a proposal
    r = store.demote("igor-2", "access_paths_from_igor", "port closed", evidence_source="tool:nc -z -> closed")
    assert r.written, r.reason
    assert store.locked_for_prompt("reach igor-2") == []


def test_migration_adds_columns_to_a_pre_423_store(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE facts (
        entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL,
        confidence TEXT NOT NULL CHECK(confidence IN ('verified', 'high_probability', 'false')),
        evidence_source TEXT NOT NULL, source_memory_id TEXT, source_agent TEXT,
        created_at REAL NOT NULL, last_updated REAL NOT NULL, PRIMARY KEY (entity, attribute));
    INSERT INTO facts VALUES ('igor', 'role', 'launchpad', 'verified', 'statement:guy', NULL, 'cc', 1.0, 1.0);
    """)
    conn.commit(); conn.close()
    store = FactsStore(db)
    f = store.get("igor", "role")
    assert f.authority == "open" and f.probe_cmd is None
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(facts)")}
    assert {"authority", "probe_cmd", "probe_host"} <= cols
    # and the old row behaves as open
    assert store.save("igor", "role", "launchpad", "verified", "dream:x").reason == "reasserted"
    # reopening is idempotent
    FactsStore(db).get("igor", "role")


# ── review 2026-09-15 on 7fda6c4 ──────────────────────────────────────────

def test_demote_on_a_locked_slot_is_a_write_like_any_other(store):
    """#1: the demote tool was the one door around every lock."""
    _seed_locked(store, "probe")
    r = store.demote("igor-2", "access_paths_from_igor", "I think it's Tailscale only", changed_by="rocky")
    assert r.written is False and r.was_contradiction is True and r.authority == "probe"
    assert r.reason.startswith("locked:probe — demote recorded as proposal #")
    pid = r.proposal_id
    f = store.get("igor-2", "access_paths_from_igor")
    assert f.confidence == "verified" and f.value == "SSH+RDP over Tailscale AND LAN"   # untouched
    assert store.locked_for_prompt("reach igor-2")                                    # still speaks
    p = store.proposals()[0]
    assert (p["id"], p["confidence"], p["proposed_value"]) == (pid, "false", f.value)
    assert p["evidence_source"] == "agent:rocky — demote: I think it's Tailscale only"
    # wrong-KIND evidence still carries the reason — Guy resolves from this row
    # same slot, same intent -> dedupes onto #1, and the row now names the latest asker
    r2 = store.demote("igor-2", "access_paths_from_igor", "statusline red", changed_by="dave",
                      evidence_source="memory:abc")
    assert r2.proposal_id == pid and r2.reason.endswith("(seen 2x)")
    p = store.proposals()[0]
    assert (p["evidence_source"], p["source_agent"], p["seen_count"]) == ("memory:abc — demote: statusline red", "dave", 2)

    # Guy's word on the demote proposal lands it as FALSE, not verified
    r = store.resolve_proposal(pid, "accept", by="cc", reason="Guy: yes, drop it")
    assert r.written is True and r.was_contradiction is False
    assert store.get("igor-2", "access_paths_from_igor", include_false=True).confidence == "false"
    assert store.locked_for_prompt("reach igor-2") == []
    hist = store.history("igor-2", "access_paths_from_igor")
    assert hist[0]["new_confidence"] == "false" and "accepted by cc" in hist[0]["reason"]
    # early returns still name the tier
    assert store.demote("igor-2", "access_paths_from_igor", "again", evidence_source="tool:x").authority == "probe"

    # the machine may demote a probe slot directly; Guy may demote a declared one
    store.save("igor-2", "bios_version", "1.2.3", "verified", "tool:dmidecode", source_agent="cc")
    store.set_authority("igor-2", "bios_version", "probe", "statement:guy", probe_cmd="dmidecode")
    assert store.demote("igor-2", "bios_version", "reflashed", evidence_source="tool:dmidecode -> 1.3.0").written
    store.save("cc", "daily_model", "fable", "verified", "statement:guy", source_agent="cc")
    store.set_authority("cc", "daily_model", "declared", "statement:guy")
    assert store.demote("cc", "daily_model", "flipped", evidence_source="tool:statusline").written is False
    assert store.demote("cc", "daily_model", "flipped", evidence_source="statement:guy S338").written
    # open slots: unchanged behaviour, no evidence needed
    store.save("guy", "city", "HMB", "verified", "memory:x", source_agent="cc")
    assert store.demote("guy", "city", "moved").written


def test_identical_held_writes_collapse_onto_one_proposal(store):
    """#4: the dreamer re-extracts the same sentence nightly."""
    _seed_locked(store, "probe")
    r1 = store.save("igor-2", "access_paths_from_igor", "Tailscale only", "high_probability",
                    "dream:2026-09-16", source_agent="dreamer")
    r2 = store.save("igor-2", "access_paths_from_igor", "Tailscale only", "high_probability",
                    "dream:2026-09-17", source_agent="dreamer")
    r3 = store.save("igor-2", "access_paths_from_igor", "Tailscale only", "high_probability",
                    "dream:2026-09-18", source_agent="dreamer")
    assert r1.proposal_id == r2.proposal_id == r3.proposal_id == 1
    assert r1.reason.endswith("recorded as proposal #1")
    assert r3.reason.endswith("already proposed as #1 (seen 3x)")
    pend = store.proposals()
    assert len(pend) == 1 and pend[0]["seen_count"] == 3 and pend[0]["last_seen"] >= pend[0]["created_at"]
    assert pend[0]["evidence_source"] == "dream:2026-09-18"     # the row names the LATEST asker
    # one history row for the proposal, not three
    assert sum(1 for h in store.history("igor-2", "access_paths_from_igor") if h["reason"].startswith("proposal #")) == 1
    # a different value is a different proposal; a demote of the same slot is its own row too
    assert store.save("igor-2", "access_paths_from_igor", "LAN only", "high_probability",
                      "dream:x", source_agent="dreamer").proposal_id == 2
    assert store.demote("igor-2", "access_paths_from_igor", "gone", changed_by="dreamer").proposal_id == 3
    assert len(store.proposals()) == 3
    # once resolved, the same write opens a NEW proposal (pending-only dedupe)
    store.resolve_proposal(1, "reject", by="cc", reason="wrong")
    assert store.save("igor-2", "access_paths_from_igor", "Tailscale only", "high_probability",
                      "dream:2026-09-19", source_agent="dreamer").proposal_id == 4


def test_accepting_a_stale_demote_proposal_never_reverts_a_correction(store):
    """Round 2 #2: a demote proposal froze the value at proposal time."""
    store.save("cc", "daily_model", "OLD VALUE", "verified", "statement:guy", source_agent="cc")
    store.set_authority("cc", "daily_model", "declared", "statement:guy")
    pid = store.demote("cc", "daily_model", "looks wrong", changed_by="rocky").proposal_id
    assert store.save("cc", "daily_model", "NEW CORRECT VALUE", "verified", "statement:guy S338",
                      source_agent="cc").written
    r = store.resolve_proposal(pid, "accept", by="cc", reason="Guy: fine, drop it")
    assert r.written and r.was_contradiction is False
    f = store.get("cc", "daily_model", include_false=True)
    assert (f.value, f.confidence) == ("NEW CORRECT VALUE", "false")   # marked false, not reverted
    # and a rejected demote leaves the slot exactly as it was
    store.save("cc", "daily_model", "NEWER", "verified", "statement:guy", source_agent="cc")
    pid2 = store.demote("cc", "daily_model", "nope", changed_by="dave").proposal_id
    store.resolve_proposal(pid2, "reject", by="cc", reason="Guy: keep it")
    f = store.get("cc", "daily_model")
    assert (f.value, f.confidence) == ("NEWER", "verified")


def test_migration_adds_dedupe_columns_to_a_4230_proposals_table(tmp_path):
    """Round 2 #1: a 4.23.0-shaped fact_proposals (no seen_count/last_seen)
    must not 500 every held write."""
    db = tmp_path / "p.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE facts (entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL,
        confidence TEXT NOT NULL, evidence_source TEXT NOT NULL, source_memory_id TEXT, source_agent TEXT,
        created_at REAL NOT NULL, last_updated REAL NOT NULL, authority TEXT NOT NULL DEFAULT 'open',
        probe_cmd TEXT, probe_host TEXT, PRIMARY KEY (entity, attribute));
    CREATE TABLE fact_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL,
        attribute TEXT NOT NULL, proposed_value TEXT NOT NULL, confidence TEXT NOT NULL,
        evidence_source TEXT NOT NULL, source_agent TEXT, authority TEXT NOT NULL, current_value TEXT,
        status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, resolved_at REAL,
        resolved_by TEXT, resolution_reason TEXT);
    INSERT INTO fact_proposals (entity, attribute, proposed_value, confidence, evidence_source, authority, created_at)
        VALUES ('igor-2', 'x', 'v', 'high_probability', 'dream:old', 'probe', 1.0);
    """)
    conn.commit(); conn.close()
    store = FactsStore(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(fact_proposals)")}
    assert {"seen_count", "last_seen"} <= cols
    assert store.proposals()[0]["seen_count"] == 1          # the old row got the default
    _seed_locked(store, "probe")
    r = store.save("igor-2", "access_paths_from_igor", "Tailscale only", "high_probability", "dream:x")
    assert r.proposal_id == 2 and store.demote("igor-2", "access_paths_from_igor", "x").proposal_id == 3
