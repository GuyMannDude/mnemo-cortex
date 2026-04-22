"""Validation for incoming observations.

Phase 1.5 rewrite. Scans proposed_claim + every evidence excerpt + metadata
integrity. Routes findings through named detectors and the policy layer to
produce a disposition (hard_block | local_only | review_required | allow)
plus the taint/provenance/salvageability metadata Gate 2 needs.

Authority: Opie's Phase 1.5 kickstart; AL's 3-pass design review.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from passport import config, detectors
from passport.detectors import private_dict
from passport.models import Observation


MAX_CLAIM_CHARS = 180
MAX_SESSION_ID_CHARS = 128
DUPLICATE_SIMILARITY_THRESHOLD = 0.85

DISPOSITION_ORDER = ("allow", "review_required", "local_only", "hard_block")


GENERIC_FLUFF_PATTERNS = [
    r"\buser is awesome\b",
    r"\buser is probably\b",
    r"\buser likes innovation\b",
    r"\buser values quality and speed\b",
    r"\buser is creative and detail-oriented\b",
    r"\buser is passionate\b",
    r"\buser is a\s+\w+\s+person\b",
]


@dataclass
class ValidationResult:
    disposition: str = "allow"
    reason_codes: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    flagged_spans: list[dict] | None = None
    redacted_claim: str | None = None
    evidence_trust: str | None = None
    salvageability: str = "none"       # none | redactable | rewriteable
    taint_flags: list[str] = field(default_factory=list)
    portability: str = "portable"      # portable | local_only | blocked
    redaction_applied: bool = False

    # Back-compat shims for callers from Phase 1 (promotion.py, tests).
    @property
    def ok(self) -> bool:
        return self.disposition == "allow"

    @property
    def reason(self) -> str | None:
        return self.reason_codes[0] if self.reason_codes else None

    def to_snapshot(self) -> dict:
        """Serializable snapshot for persisting alongside the pending observation."""
        return {
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
            "duplicate_of": self.duplicate_of,
            "flagged_spans": list(self.flagged_spans or []),
            "redacted_claim": self.redacted_claim,
            "evidence_trust": self.evidence_trust,
            "salvageability": self.salvageability,
            "taint_flags": list(self.taint_flags),
            "portability": self.portability,
            "redaction_applied": self.redaction_applied,
        }


# ─── Helpers ────────────────────────────────────────────────────────────────

def _strongest(disps) -> str:
    best = "allow"
    best_rank = 0
    for d in disps:
        try:
            rank = DISPOSITION_ORDER.index(d)
        except ValueError:
            continue
        if rank > best_rank:
            best = d
            best_rank = rank
    return best


def _portability(disposition: str) -> str:
    if disposition == "hard_block":
        return "blocked"
    if disposition == "local_only":
        return "local_only"
    return "portable"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _iter_active_claims(stable: dict):
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
    if len(claim.strip().split()) < 3:
        return True
    return False


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


_SESSION_ID_OK = re.compile(r"^[\x20-\x7E]{1,%d}$" % MAX_SESSION_ID_CHARS)


def _session_id_valid(sid: str) -> bool:
    return bool(sid) and bool(_SESSION_ID_OK.match(sid))


# ─── Main entry ─────────────────────────────────────────────────────────────

def validate_observation(obs: Observation, stable: dict) -> ValidationResult:
    policy = config.load_policy()
    dispositions_map: dict = policy.get("dispositions", {})
    bucket_defaults: dict = policy.get("bucket_defaults", {})
    rules: dict = policy.get("rules", {})

    reason_codes: list[str] = []
    flagged_spans: list[dict] = []
    taint_flags: list[str] = []
    dispositions_seen: set[str] = set()

    # (1) Length cap (defence in depth for dict-constructed Observations).
    if len(obs.proposed_claim) > MAX_CLAIM_CHARS:
        return ValidationResult(disposition="hard_block", reason_codes=["claim_too_long"])

    if len(obs.evidence) < 2:
        disp = dispositions_map.get("insufficient_evidence", "hard_block")
        return ValidationResult(disposition=disp, reason_codes=["insufficient_evidence"])

    # (2) Metadata integrity.
    source_bucket, meta_untrusted = config.resolve_bucket(obs.source_platform)
    if meta_untrusted:
        taint_flags.append("metadata_untrusted")
        reason_codes.append("metadata_integrity:unknown_source_platform")
    if not _session_id_valid(obs.source_session_id):
        if "metadata_untrusted" not in taint_flags:
            taint_flags.append("metadata_untrusted")
        reason_codes.append("metadata_integrity:malformed_session_id")

    # (3) Per-evidence provenance + observation-level trust.
    evidence_buckets: list[str] = []
    for ev in obs.evidence:
        bucket = ev.provenance_bucket
        if not bucket or bucket not in config.BUCKET_RANK:
            bucket = source_bucket  # fall back to observation-level
        evidence_buckets.append(bucket)
    evidence_trust = config.weakest_bucket(evidence_buckets)
    if "metadata_untrusted" in taint_flags:
        evidence_trust = "metadata_untrusted"

    # (4) Run named detectors over claim + every evidence excerpt.
    scoped: list[tuple[str, dict]] = []
    for f in detectors.scan_text(obs.proposed_claim):
        scoped.append(("claim", f))
    for i, ev in enumerate(obs.evidence):
        for f in detectors.scan_text(ev.excerpt or ""):
            scoped.append((f"evidence[{i}]", f))

    # (5) Classify each finding → disposition.
    for loc, f in scoped:
        cat = f["category"]
        if cat == "injection":
            specialized = "injection_in_claim" if loc == "claim" else "injection_in_evidence"
            disp = dispositions_map.get(specialized, "review_required")
            reason_codes.append(f"{specialized}:{f['detector_id']}@{loc}")
            if loc != "claim" and "untrusted_instructional_text" not in taint_flags:
                taint_flags.append("untrusted_instructional_text")
        else:
            disp = dispositions_map.get(cat, f.get("severity", "review_required"))
            reason_codes.append(f"{cat}:{f['detector_id']}@{loc}")
        dispositions_seen.add(disp)
        flagged_spans.append({
            "detector_id": f["detector_id"],
            "category": cat,
            "disposition": disp,
            "location": loc,
            "start": f["start"],
            "end": f["end"],
            "label": f["label"],
            "match": f["match"],
        })

    # (6) Generic fluff (claim only).
    if _is_generic_fluff(obs.proposed_claim):
        dispositions_seen.add(dispositions_map.get("generic_fluff", "hard_block"))
        reason_codes.append("generic_fluff")

    # (7) Duplicate of active claim.
    dup = _find_duplicate(obs.proposed_claim, stable)
    if dup:
        dispositions_seen.add(dispositions_map.get("duplicate", "hard_block"))
        reason_codes.append(f"duplicate_of_active_claim:{dup}")

    # (8) Redaction pass — only for private_dict hits in the claim.
    redacted_claim: str | None = None
    salvageability = "none"
    redaction_applied = False
    claim_has_private_dict = any(
        (loc == "claim" and f["category"] == "private_dict") for loc, f in scoped
    )
    if claim_has_private_dict:
        candidate, changed = private_dict.try_redact(obs.proposed_claim)
        if changed:
            residual = [
                f for f in detectors.scan_text(candidate)
                if f["category"] == "private_dict"
            ]
            if not residual:
                redacted_claim = candidate
                redaction_applied = True
                salvageability = "redactable"
                # Redaction de-escalates hard_block to review_required so a
                # human can sign off on the noun→category mapping.
                dispositions_seen.discard("hard_block")
                dispositions_seen.add("review_required")
                reason_codes.append("redaction:private_dict_salvaged")

    # (9) Final disposition = strongest seen, floored by bucket default.
    # Emit an audit code when the floor actually raises the outcome, so
    # reviewers can trace "why did this land at local_only?" back to the
    # trust bucket rather than guessing.
    pre_floor = _strongest(dispositions_seen) if dispositions_seen else "allow"
    bucket_floor = bucket_defaults.get(evidence_trust, "allow")
    final = _strongest([pre_floor, bucket_floor])
    if final != pre_floor:
        reason_codes.append(f"bucket_floor:{evidence_trust}={bucket_floor}")

    # (10) Untrusted-alone rule for shared-scope promotion.
    # If every evidence row is untrusted_web, cap the disposition at local_only
    # regardless of what detectors said about the content — the data cannot be
    # trusted enough to promote to the shared passport.
    unique_buckets = set(evidence_buckets)
    if (
        unique_buckets == {"untrusted_web"}
        and not rules.get("untrusted_alone_can_promote_shared", False)
        and final == "allow"
    ):
        final = "local_only"
        reason_codes.append("provenance:untrusted_web_alone")

    return ValidationResult(
        disposition=final,
        reason_codes=reason_codes,
        duplicate_of=dup,
        flagged_spans=flagged_spans or None,
        redacted_claim=redacted_claim,
        evidence_trust=evidence_trust,
        salvageability=salvageability,
        taint_flags=taint_flags,
        portability=_portability(final),
        redaction_applied=redaction_applied,
    )
