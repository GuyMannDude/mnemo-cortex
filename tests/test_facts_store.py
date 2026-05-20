"""Tests for FactsStore (Mnemo v4 Phase 3)."""
from __future__ import annotations

import pytest

from agentb.facts_store import FactsStore, CONFIDENCE_LEVELS


@pytest.fixture
def store(tmp_path):
    return FactsStore(tmp_path / "facts.sqlite")


def test_initial_insert(store):
    r = store.save("Guy", "location", "Half Moon Bay", "verified", "statement:Guy direct", source_agent="cc")
    assert r.written is True
    assert r.was_contradiction is False
    f = store.get("guy", "location")
    assert f is not None
    assert f.value == "Half Moon Bay"
    assert f.confidence == "verified"


def test_entity_attribute_normalization(store):
    store.save("Guy", "Home Location", "HMB", "verified", "statement:Guy", source_agent="cc")
    assert store.get("GUY", "Home Location") is not None
    assert store.get("guy", "home_location") is not None
    assert store.get("Guy", "home-location") is not None


def test_get_excludes_false_by_default(store):
    store.save("guy", "city", "Pacifica", "verified", "statement:Guy", source_agent="cc")
    store.demote("guy", "city", "Guy says he moved", changed_by="cc")
    assert store.get("guy", "city") is None
    assert store.get("guy", "city", include_false=True) is not None


def test_get_missing_returns_none(store):
    assert store.get("nobody", "nothing") is None


def test_reasserted_same_value_promotes_confidence(store):
    store.save("guy", "location", "HMB", "high_probability", "dream:2026-05-20", source_agent="dreamer")
    r = store.save("guy", "location", "HMB", "verified", "statement:Guy direct", source_agent="cc")
    assert r.written is True
    assert r.was_contradiction is False
    f = store.get("guy", "location")
    assert f.confidence == "verified"
    assert f.evidence_source == "statement:Guy direct"


def test_reasserted_same_value_does_not_demote(store):
    store.save("guy", "location", "HMB", "verified", "statement:Guy direct", source_agent="cc")
    r = store.save("guy", "location", "HMB", "high_probability", "dream:2026-05-21", source_agent="dreamer")
    assert r.written is True
    f = store.get("guy", "location")
    assert f.confidence == "verified"  # not demoted


def test_contradiction_higher_overwrites(store):
    store.save("guy", "city", "HMB", "high_probability", "dream:old", source_agent="dreamer")
    r = store.save("guy", "city", "Pacifica", "verified", "statement:Guy direct", source_agent="cc")
    assert r.written is True
    assert r.was_contradiction is True
    assert r.previous_value == "HMB"
    assert store.get("guy", "city").value == "Pacifica"


def test_contradiction_lower_rejected(store):
    store.save("guy", "city", "HMB", "verified", "statement:Guy", source_agent="cc")
    r = store.save("guy", "city", "Pacifica", "high_probability", "dream:weak", source_agent="dreamer")
    assert r.written is False
    assert r.was_contradiction is True
    assert r.previous_value == "HMB"
    assert store.get("guy", "city").value == "HMB"  # unchanged


def test_contradiction_equal_overwrites(store):
    store.save("guy", "city", "HMB", "verified", "statement:Guy 2026-01", source_agent="cc")
    r = store.save("guy", "city", "Pacifica", "verified", "statement:Guy 2026-05 moved", source_agent="cc")
    assert r.written is True
    assert r.was_contradiction is True
    assert store.get("guy", "city").value == "Pacifica"


def test_demote_marks_false(store):
    store.save("guy", "city", "HMB", "verified", "statement:Guy", source_agent="cc")
    r = store.demote("guy", "city", "Guy says wrong", changed_by="cc")
    assert r.written is True
    assert r.previous_confidence == "verified"
    f = store.get("guy", "city", include_false=True)
    assert f.confidence == "false"


def test_demote_missing_fact(store):
    r = store.demote("ghost", "nothing", "reason")
    assert r.written is False
    assert r.reason == "no such fact"


def test_demote_already_false_is_noop(store):
    store.save("guy", "city", "HMB", "verified", "statement:Guy", source_agent="cc")
    store.demote("guy", "city", "first demote")
    r = store.demote("guy", "city", "second demote")
    assert r.written is False
    assert r.reason == "already false"


def test_demote_requires_reason(store):
    store.save("guy", "city", "HMB", "verified", "statement:Guy", source_agent="cc")
    with pytest.raises(ValueError):
        store.demote("guy", "city", "")


def test_save_requires_evidence_source(store):
    with pytest.raises(ValueError):
        store.save("guy", "city", "HMB", "verified", "", source_agent="cc")


def test_save_validates_confidence(store):
    with pytest.raises(ValueError):
        store.save("guy", "city", "HMB", "bogus", "statement:Guy", source_agent="cc")


def test_query_by_entity(store):
    store.save("guy", "city", "HMB", "verified", "x", source_agent="cc")
    store.save("guy", "github_org", "GuyMannDude", "verified", "x", source_agent="cc")
    store.save("rocky", "model", "deepseek", "verified", "x", source_agent="cc")
    results = store.query(entity="guy")
    assert len(results) == 2
    assert {r.attribute for r in results} == {"city", "github_org"}


def test_query_by_confidence(store):
    store.save("a", "x", "1", "verified", "e", source_agent="cc")
    store.save("b", "x", "2", "high_probability", "e", source_agent="cc")
    store.save("c", "x", "3", "high_probability", "e", source_agent="cc")
    high = store.query(confidence="high_probability")
    assert len(high) == 2
    verified = store.query(confidence="verified")
    assert len(verified) == 1


def test_query_value_contains(store):
    store.save("guy", "city", "Half Moon Bay", "verified", "e", source_agent="cc")
    store.save("rocky", "model", "google/gemini-2.5-flash", "verified", "e", source_agent="cc")
    results = store.query(value_contains="gemini")
    assert len(results) == 1
    assert results[0].entity == "rocky"


def test_query_limit(store):
    for i in range(30):
        store.save(f"e{i}", "x", str(i), "verified", "e", source_agent="cc")
    assert len(store.query(limit=5)) == 5
    assert len(store.query(limit=100)) == 30


def test_history_records_changes(store):
    store.save("guy", "city", "HMB", "high_probability", "dream:1", source_agent="dreamer")
    store.save("guy", "city", "HMB", "verified", "statement:Guy", source_agent="cc")
    store.save("guy", "city", "Pacifica", "verified", "statement:Guy moved", source_agent="cc")
    h = store.history("guy", "city")
    assert len(h) == 3
    assert h[0]["reason"] == "contradicted by new evidence"
    assert h[1]["reason"] == "reasserted"
    assert h[2]["reason"] == "initial assertion"


def test_contradictions_view(store):
    store.save("guy", "city", "HMB", "verified", "statement:Guy", source_agent="cc")
    store.save("guy", "city", "Pacifica", "high_probability", "dream:weak", source_agent="dreamer")
    store.save("guy", "github_org", "old", "high_probability", "dream:1", source_agent="dreamer")
    store.save("guy", "github_org", "GuyMannDude", "verified", "statement:Guy", source_agent="cc")
    c = store.contradictions()
    # Should include the rejected lower-confidence + the overwritten higher-confidence
    reasons = [r["reason"] for r in c]
    assert any("rejected" in r for r in reasons)
    assert any("contradicted" in r for r in reasons)


def test_confidence_constants():
    assert CONFIDENCE_LEVELS == ("false", "high_probability", "verified")


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "facts.sqlite"
    s1 = FactsStore(path)
    s1.save("guy", "city", "HMB", "verified", "e", source_agent="cc")
    s2 = FactsStore(path)
    f = s2.get("guy", "city")
    assert f is not None
    assert f.value == "HMB"
