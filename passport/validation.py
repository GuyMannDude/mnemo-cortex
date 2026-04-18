"""Validation gates for incoming observations.

Authority: AL §8 reject list + CC spec credential patterns.
Pure function — no I/O except reading the `stable` dict passed in.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from passport.models import Observation


MAX_CLAIM_CHARS = 180
DUPLICATE_SIMILARITY_THRESHOLD = 0.85


GENERIC_FLUFF_PATTERNS = [
    r"\buser is awesome\b",
    r"\buser is probably\b",
    r"\buser likes innovation\b",
    r"\buser values quality and speed\b",
    r"\buser is creative and detail-oriented\b",
    r"\buser is passionate\b",
    r"\buser is a\s+\w+\s+person\b",
]

SECRET_PATTERNS = [
    (r"\bsk-[A-Za-z0-9_\-]{20,}\b", "openai-style api key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "aws access key id"),
    (r"\bghp_[A-Za-z0-9]{36}\b", "github personal access token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "slack token"),
    (r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----", "private key block"),
]

CREDENTIAL_KEYWORDS = [
    r"\bpassword\s*[:=]",
    r"\bapi[_\-]?key\s*[:=]",
    r"\bsecret\s*[:=]",
    r"\btoken\s*[:=]",
]


@dataclass
class ValidationResult:
    ok: bool
    reason: str | None = None
    duplicate_of: str | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _iter_active_claims(stable: dict):
    """Yield (claim_id, claim_text) for every active claim in the stable doc."""
    core = stable.get("stable_core", {}) or {}
    for _section_name, items in core.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if item.get("status") == "active":
                yield item.get("claim_id"), item.get("claim", "")
    for item in stable.get("negative_constraints", []) or []:
        if item.get("status") == "active":
            yield item.get("claim_id"), item.get("claim", "")


def _is_generic_fluff(claim: str) -> bool:
    low = claim.lower()
    for pat in GENERIC_FLUFF_PATTERNS:
        if re.search(pat, low):
            return True
    # Very-short / adjective-only claims are fluff in disguise.
    if len(claim.strip().split()) < 3:
        return True
    return False


def _contains_secret(claim: str) -> tuple[bool, str | None]:
    for pat, label in SECRET_PATTERNS:
        if re.search(pat, claim):
            return True, label
    for pat in CREDENTIAL_KEYWORDS:
        if re.search(pat, claim, re.IGNORECASE):
            return True, "credential-like keyword"
    return False, None


def _find_duplicate(claim: str, stable: dict) -> str | None:
    target = _normalize(claim)
    best_id = None
    best_ratio = 0.0
    for cid, text in _iter_active_claims(stable):
        ratio = difflib.SequenceMatcher(None, target, _normalize(text)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = cid
    if best_ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
        return best_id
    return None


def validate_observation(obs: Observation, stable: dict) -> ValidationResult:
    # (1) Evidence count — Pydantic already enforces min_length=2, but double-check
    # in case a dict was built off-model somewhere upstream.
    if len(obs.evidence) < 2:
        return ValidationResult(False, "insufficient_evidence")

    # (2) Length cap. Pydantic enforces at construction too; this is defence-in-depth
    #     for dict-constructed observations that bypass the model.
    if len(obs.proposed_claim) > MAX_CLAIM_CHARS:
        return ValidationResult(False, "claim_too_long")

    # (3) Secret / credential scan — run BEFORE the fluff check, because a very short
    #     credential-bearing claim would otherwise get misreported as "generic_fluff".
    has_secret, label = _contains_secret(obs.proposed_claim)
    if has_secret:
        return ValidationResult(False, f"contains_credential:{label}")

    # (4) Generic fluff reject list.
    if _is_generic_fluff(obs.proposed_claim):
        return ValidationResult(False, "generic_fluff")

    # (5) Duplicate of existing active claim.
    dup = _find_duplicate(obs.proposed_claim, stable)
    if dup:
        return ValidationResult(False, "duplicate_of_active_claim", duplicate_of=dup)

    return ValidationResult(True)
