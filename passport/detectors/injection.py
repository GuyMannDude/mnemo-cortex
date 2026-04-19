"""Prompt-injection phrase detector.

Fires on phrases that try to steer the validator/agent away from its policy.
Severity is keyed to WHERE the phrase appears:

    in the proposed_claim   → hard_block        (the user can't promote
                                                 "save this to memory" as
                                                 their own preference)
    in an evidence excerpt  → review_required + taint "untrusted_instructional_text"

Category `injection` is a single detector (`injection_phrase`); the validator
chooses between `injection_in_claim` and `injection_in_evidence` at scan time.
"""
from __future__ import annotations

import re

from passport.detectors import Detector


_PHRASES: list[str] = [
    "ignore previous instructions",
    "ignore prior instructions",
    "disregard previous instructions",
    "save this to memory",
    "add this to memory",
    "add this to passport",
    "save this to passport",
    "add this to your profile",
    "update your profile",
    "update the user profile",
    "the user prefers",
    "remember that the user",
    "write that the user",
    "store this preference",
    "store this as a preference",
    "store the following preference",
    "override your system prompt",
    "reveal your system prompt",
    "act as if the user",
]


def _build_pattern() -> re.Pattern[str]:
    alts = "|".join(re.escape(p) for p in _PHRASES)
    return re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)


_PATTERN = _build_pattern()


def scan(text: str) -> list[dict]:
    out: list[dict] = []
    for m in _PATTERN.finditer(text):
        out.append({
            "detector_id": "injection_phrase",
            "category": "injection",           # validator specializes by location
            "severity": "review_required",     # baseline; validator may escalate
            "start": m.start(),
            "end": m.end(),
            "label": "prompt injection phrase",
            "match": m.group(),
        })
    return out


DETECTORS: list[Detector] = [
    Detector(
        detector_id="injection_phrase",
        category="injection",
        default_severity="review_required",
        scan_fn=scan,
        description="prompt injection phrase",
    )
]
