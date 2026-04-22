"""Adapter: AL/Codex v0-spec corpus → Phase 1 Observation objects.

AL's PR #3 used the original Mnemo Passport v0 spec field names
(`pattern`, `context`, `quote`). Phase 1 shipped with `proposed_claim`,
`excerpt`, plus additional required fields. This module maps between them
so the corpus can be evaluated without rewriting 200 YAML entries.

Key translations:
    observation.pattern          → proposed_claim
    observation.context          → tags[0] (best available home)
    observation.confidence (str) → float (low=0.3, medium=0.6, high=0.9)
    evidence[].quote             → evidence[].excerpt

Fields synthesized when missing:
    observation.observation_id       ← corpus entry `id`
    observation.type                 ← "preference" (default)
    observation.proposed_target_section ← "stable_core.communication"
    evidence[].evidence_id           ← f"{obs_id}_ev_{i}"
    evidence[].session_id            ← source_session_id
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import yaml

from passport.models import ClaimType, Evidence, Observation


CONFIDENCE_MAP = {"low": 0.3, "medium": 0.6, "high": 0.9}


def _confidence_to_float(v) -> float:
    if isinstance(v, (int, float)):
        return float(max(0.0, min(1.0, v)))
    return CONFIDENCE_MAP.get(str(v).strip().lower(), 0.6)


def migrate_entry(raw: dict) -> tuple[Observation, str, str]:
    """Convert one AL-format entry into (Observation, expected_label, rationale).

    Uses `model_construct` so we can produce Observations that would fail
    pydantic validation (e.g. single-evidence edge cases) — the validator
    handles those; pydantic rejection would mask the test.
    """
    obs_id = raw["id"]
    label = raw["label"]
    rationale = raw.get("rationale", "")
    o = raw["observation"]

    ev_objs = []
    for i, ev in enumerate(o.get("evidence", []) or []):
        ev_objs.append(Evidence.model_construct(
            evidence_id=f"{obs_id}_ev_{i}",
            session_id=o["source_session_id"],
            turn_ref=ev.get("turn_ref", f"{obs_id}:turn-{i}"),
            excerpt=ev.get("quote", ""),
            origin_type=ev.get("origin_type"),
            provenance_bucket=ev.get("provenance_bucket"),
            capture_mode=None,
            origin_uri_hash=None,
            taint_flags=[],
            redacted_excerpt=None,
        ))

    # context goes into tags as a lightweight carrier — Observation has no
    # context field, and tags are ignored by the validator anyway so this
    # is zero-risk.
    tags = [o["context"]] if o.get("context") else []

    observation = Observation.model_construct(
        observation_id=obs_id,
        proposed_claim=o.get("pattern", ""),
        type=ClaimType.preference,
        scope=[],
        confidence=_confidence_to_float(o.get("confidence")),
        status="pending",
        proposed_action="promote",
        proposed_target_section="stable_core.communication",
        source_platform=o.get("source_platform", ""),
        source_session_id=o.get("source_session_id", ""),
        evidence=ev_objs,
    )
    # Carry tags as an attribute pydantic didn't declare — validator doesn't read it.
    observation.__dict__["tags"] = tags
    return observation, label, rationale


def iter_corpus(corpus_dir: Path | None = None) -> Iterator[tuple[str, Observation, str, str]]:
    """Yield (source_file_stem, observation, expected_label, rationale) for every entry."""
    if corpus_dir is None:
        corpus_dir = Path(__file__).parent / "corpus"
    for name in ("benign", "toxic", "edge", "adversarial"):
        path = corpus_dir / f"{name}.yaml"
        if not path.exists():
            continue
        entries = yaml.safe_load(open(path)) or []
        for entry in entries:
            obs, label, rationale = migrate_entry(entry)
            yield name, obs, label, rationale
