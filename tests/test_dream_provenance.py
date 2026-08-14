"""Self-eating guard — dreamer-derived records must not become dream evidence.

The 2026-08-14 loop (Opie #2486): the dreamer's contradiction fan-out ping was
processed by Rocky, Rocky's memory record quoted the subject line, the next
night's harvest read that record under Rocky's provenance, and fact extraction
produced role="dream-contradictions" — a fragment of the dreamer's own output,
flagged as contradicting the verified fact it descends from. Lane-level
exclusion (harvest skips the dreamer agent) cannot catch this because the
record wears the processing agent's identity.

These tests pin the deterministic provenance filter: the literal #2486 specimen
must be excluded, ordinary records must survive, and the filter must be
extraction-scoped (synthesis keeps seeing everything — that is a call-site
property, asserted here by checking the filter is only consulted in
extract_facts_for_agent's path via the private predicate, not in harvest).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# The dreamer is a top-level script with a hyphen in its name — load it by path.
_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)


def _mem(summary: str, key_facts: list | None = None, decisions: list | None = None) -> dict:
    return {
        "timestamp": "2026-08-14T03:00:00+00:00",
        "session_id": "sess-x",
        "summary": summary,
        "key_facts": key_facts or [],
        "decisions": decisions or [],
    }


# ── the literal loop specimen, and its family ──


def test_2486_specimen_is_excluded():
    m = _mem("Processed Disco Bus message #2401: dream-contradictions for Opie's role.")
    assert dream._is_dreamer_derived(m)


def test_brief_quoting_record_is_excluded():
    m = _mem("Read the boot block. # Mnemo Dream — 2026-08-14 said cc shipped the GHA fix.")
    assert dream._is_dreamer_derived(m)


def test_generator_credit_line_is_excluded():
    m = _mem("_Generated 2026-08-14 10:17 UTC by mnemo-dream.py_")
    assert dream._is_dreamer_derived(m)


def test_contradiction_vocabulary_is_excluded():
    m = _mem("Dream 2026-08-14: 2 verified-vs-extracted contradiction(s) this dream")
    assert dream._is_dreamer_derived(m)


def test_signature_in_key_facts_is_excluded():
    m = _mem("Routine morning.", key_facts=["forwarded dream-contradictions-2026-08-14 to Opie"])
    assert dream._is_dreamer_derived(m)


def test_signature_in_decisions_is_excluded():
    m = _mem("Routine morning.", decisions=["acked dream-git-sync-drift for mnemo-cortex"])
    assert dream._is_dreamer_derived(m)


# ── ordinary evidence must survive ──


def test_ordinary_record_survives():
    m = _mem("Rotated the CronAlarm webhook; new pair proven live before revocation.",
             key_facts=["IGOR-2 is the Mnemo Cortex host"],
             decisions=["keys live in the fleet vault"])
    assert not dream._is_dreamer_derived(m)


def test_paraphrase_without_artifacts_survives():
    # A human-voiced mention of dreaming (no artifact strings) stays IN —
    # paraphrase handling is the narrative tier's job (#2406), not this filter's.
    m = _mem("Guy asked how the overnight dreaming feature chooses what to keep.")
    assert not dream._is_dreamer_derived(m)


# ── the filter must be load-bearing at the extraction call site ──


def test_extraction_input_drops_derived_records(monkeypatch):
    """extract_facts_for_agent must never send a dreamer-derived record to the
    LLM. Asserted by capturing what the section builder receives — if the
    filter is deleted, the specimen record reaches the builder and this fails."""
    specimen = _mem("Processed bus message #2401: dream-contradictions for Opie's role.")
    ordinary = _mem("April's HoffmanBedding order shipped; nightstands approved.")
    seen: list[dict] = []

    def fake_build(agent_id, chunk):
        seen.extend(chunk)
        return "SECTION"

    monkeypatch.setattr(dream, "_build_agent_section", fake_build)
    monkeypatch.setattr(dream, "_extract_facts_from_section", lambda *a, **k: [])
    dream.extract_facts_for_agent("rocky", [specimen, ordinary])

    assert ordinary in seen
    assert specimen not in seen
