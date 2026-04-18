"""Deprecate / forget / override a stable claim."""
from __future__ import annotations

from dataclasses import dataclass

from passport import audit, git_helper, promotion, storage
from passport.models import Action, Claim, ClaimStatus, ClaimType, Durability, Evidence, utcnow


VALID_ACTIONS = {"deprecate", "forget", "override", "replace"}


@dataclass
class OverrideResult:
    success: bool
    action: str
    override_id: str | None = None
    new_claim_id: str | None = None
    reason: str | None = None
    commit_sha: str | None = None


def _iter_all_claim_sections(stable: dict):
    """Yield (container_list, idx, claim_dict) for every claim in the stable doc."""
    core = stable.get("stable_core") or {}
    for section_name, items in core.items():
        if isinstance(items, list):
            for idx, item in enumerate(items):
                yield items, idx, item, f"stable_core.{section_name}"
    negs = stable.get("negative_constraints") or []
    for idx, item in enumerate(negs):
        yield negs, idx, item, "negative_constraints"


def _find(stable: dict, claim_id: str):
    for container, idx, item, section in _iter_all_claim_sections(stable):
        if item.get("claim_id") == claim_id:
            return container, idx, item, section
    return None


def apply(
    action: str,
    target_claim_id: str,
    replacement_claim: str | None = None,
    reason: str | None = None,
    actor: str = "user",
) -> OverrideResult:
    if action == "replace":
        action = "override"
    if action not in VALID_ACTIONS:
        return OverrideResult(False, action, reason=f"invalid_action:{action}")

    with storage.exclusive_lock(storage.stable_path()):
        stable = storage.load_stable()
        hit = _find(stable, target_claim_id)
        if hit is None:
            return OverrideResult(False, action, reason="target_not_found")
        container, idx, item, section = hit

        new_claim_id: str | None = None

        if action == "deprecate":
            item["status"] = ClaimStatus.deprecated.value
            item["last_confirmed_at"] = None
            storage.save_stable(stable)

        elif action == "forget":
            # Mark forgotten and remove from the stable doc. Audit keeps the snapshot.
            item["status"] = ClaimStatus.forgotten.value
            del container[idx]
            storage.save_stable(stable)

        elif action == "override":
            if not replacement_claim:
                return OverrideResult(False, action, reason="replacement_claim_required")
            claim_type = ClaimType(item.get("type"))
            new_claim_id = promotion.mint_claim_id(claim_type, replacement_claim, stable)
            # Preserve the old claim's evidence on the replacement — the mutation is
            # a rewording, not fresh evidence. If upstream wants fresh evidence, they
            # should call `observe_behavior` + `promote_observation` instead.
            new_claim = Claim(
                claim_id=new_claim_id,
                claim=replacement_claim,
                type=claim_type,
                scope=item.get("scope") or [],
                confidence=float(item.get("confidence", 0.9)),
                status=ClaimStatus.active,
                durability=Durability(item.get("durability", "durable")),
                created_at=utcnow(),
                last_confirmed_at=utcnow(),
                source_platforms=item.get("source_platforms") or [],
                evidence=[Evidence.model_validate(e) for e in (item.get("evidence") or [])],
                tags=item.get("tags") or [],
                supersedes=[target_claim_id],
            )
            item["status"] = ClaimStatus.deprecated.value
            item["last_confirmed_at"] = None
            # Insert the new claim into the same section.
            container.append(new_claim.model_dump(mode="json"))
            storage.save_stable(stable)

    action_enum = {
        "deprecate": Action.deprecate,
        "forget": Action.forget,
        "override": Action.override,
    }[action]

    entry = audit.make_entry(
        action=action_enum,
        actor=actor,
        target_claim_id=target_claim_id,
        replacement_claim_id=new_claim_id,
        payload=item,
        reason=reason,
    )
    short = reason or action
    commit_id = new_claim_id or target_claim_id
    sha = git_helper.commit(action, commit_id, short)
    audit.append(entry, commit_sha=sha)

    return OverrideResult(
        success=True,
        action=action,
        override_id=entry.audit_id,
        new_claim_id=new_claim_id,
        commit_sha=sha,
    )
