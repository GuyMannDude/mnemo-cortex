"""Promote pending observations into stable claims."""
from __future__ import annotations

import re
from dataclasses import dataclass

from passport import audit, git_helper, pending, storage, validation
from passport.models import (
    Action,
    Claim,
    ClaimStatus,
    ClaimType,
    Durability,
    Evidence,
    Observation,
    TYPE_PREFIX,
    utcnow,
)


@dataclass
class PromotionResult:
    promoted: bool
    claim_id: str | None
    target_section: str | None
    reason: str | None = None
    commit_sha: str | None = None


def _slug_from_claim(text: str) -> str:
    """Take the first meaningful word, lowercase it, strip non-alphanumerics."""
    stop = {"a", "an", "the", "for", "and", "or", "to", "of", "in"}
    for word in text.split():
        clean = re.sub(r"[^a-z0-9]", "", word.lower())
        if clean and clean not in stop:
            return clean[:16]
    return "claim"


def _existing_ids(stable: dict) -> set[str]:
    ids: set[str] = set()
    for section in (stable.get("stable_core") or {}).values():
        if isinstance(section, list):
            for item in section:
                cid = item.get("claim_id")
                if cid:
                    ids.add(cid)
    for item in stable.get("negative_constraints") or []:
        cid = item.get("claim_id")
        if cid:
            ids.add(cid)
    return ids


def mint_claim_id(claim_type: ClaimType, claim_text: str, stable: dict) -> str:
    prefix = TYPE_PREFIX.get(claim_type, claim_type.value)
    slug = _slug_from_claim(claim_text)
    existing = _existing_ids(stable)
    n = 1
    while True:
        candidate = f"{prefix}_{slug}_{n:03d}"
        if candidate not in existing:
            return candidate
        n += 1


def _get_section_list(stable: dict, dotted: str) -> list:
    """Resolve a dotted path like 'stable_core.workflow' → a list inside `stable`.

    If the section doesn't exist yet, create it as an empty list.
    """
    parts = dotted.split(".")
    node = stable
    for key in parts[:-1]:
        sub = node.get(key)
        if not isinstance(sub, dict):
            sub = {}
            node[key] = sub
        node = sub
    leaf = parts[-1]
    if not isinstance(node.get(leaf), list):
        node[leaf] = []
    return node[leaf]


def promote(
    observation_id: str,
    target_section: str | None = None,
    actor: str = "system",
) -> PromotionResult:
    obs_dict = pending.get(observation_id)
    if obs_dict is None:
        return PromotionResult(False, None, None, reason="observation_not_found")
    if obs_dict.get("status") != "pending":
        return PromotionResult(False, None, None, reason="already_promoted")

    obs = Observation.model_validate(obs_dict)
    stable = storage.load_stable()

    # Defence in depth — validate one more time against the latest stable.
    vr = validation.validate_observation(obs, stable)
    if not vr.ok:
        return PromotionResult(False, None, None, reason=vr.reason)

    section = target_section or obs.proposed_target_section
    claim_id = mint_claim_id(obs.type, obs.proposed_claim, stable)

    claim = Claim(
        claim_id=claim_id,
        claim=obs.proposed_claim,
        type=obs.type,
        scope=obs.scope,
        confidence=obs.confidence,
        status=ClaimStatus.active,
        durability=Durability.durable,
        created_at=utcnow(),
        last_confirmed_at=utcnow(),
        source_platforms=[obs.source_platform],
        evidence=[Evidence.model_validate(e) for e in obs.evidence] if obs.evidence and isinstance(obs.evidence[0], dict) else obs.evidence,
        tags=[],
    )

    with storage.exclusive_lock(storage.stable_path()):
        stable = storage.load_stable()  # re-load under lock
        target_list = _get_section_list(stable, section)
        target_list.append(claim.model_dump(mode="json"))
        storage.save_stable(stable)

    pending.mark_promoted(observation_id)

    entry = audit.make_entry(
        Action.promote,
        actor=actor,
        target_claim_id=claim_id,
        payload=claim.model_dump(mode="json"),
        reason=f"promoted from {observation_id} into {section}",
    )
    sha = git_helper.commit("promote", claim_id, obs.proposed_claim)
    audit.append(entry, commit_sha=sha)

    return PromotionResult(True, claim_id, section, commit_sha=sha)
