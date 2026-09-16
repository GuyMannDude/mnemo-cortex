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
    # a demoted (false) locked fact does not speak
    store.demote("igor-2", "access_paths_from_igor", "re-probing")
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
