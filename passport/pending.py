"""Pending observation queue operations."""
from __future__ import annotations

from passport import storage
from passport.models import Observation


def _mint_observation_id(doc: dict) -> str:
    n = doc.get("next_counter", 1)
    doc["next_counter"] = n + 1
    return f"obs_{n:03d}"


def add(
    *,
    proposed_claim: str,
    type: str,
    scope: list[str],
    confidence: float,
    proposed_target_section: str,
    source_platform: str,
    source_session_id: str,
    evidence: list[dict],
) -> Observation:
    """Append a new pending observation. Returns the fully-populated Observation."""
    with storage.exclusive_lock(storage.pending_path()):
        doc = storage.load_pending()
        obs_id = _mint_observation_id(doc)
        obs = Observation(
            observation_id=obs_id,
            proposed_claim=proposed_claim,
            type=type,  # type: ignore[arg-type]
            scope=scope,
            confidence=confidence,
            proposed_target_section=proposed_target_section,
            source_platform=source_platform,
            source_session_id=source_session_id,
            evidence=evidence,  # type: ignore[arg-type]
        )
        doc.setdefault("pending_observations", []).append(obs.model_dump(mode="json"))
        storage.save_pending(doc)
    return obs


def list_all(status_filter: str | None = None, limit: int | None = None) -> list[dict]:
    doc = storage.load_pending()
    items = doc.get("pending_observations", [])
    if status_filter:
        items = [o for o in items if o.get("status") == status_filter]
    if limit is not None:
        items = items[:limit]
    return items


def get(observation_id: str) -> dict | None:
    for o in storage.load_pending().get("pending_observations", []):
        if o.get("observation_id") == observation_id:
            return o
    return None


def mark_promoted(observation_id: str) -> bool:
    """Flip status to 'promoted' in the pending file (keep for audit)."""
    with storage.exclusive_lock(storage.pending_path()):
        doc = storage.load_pending()
        hit = False
        for o in doc.get("pending_observations", []):
            if o.get("observation_id") == observation_id:
                o["status"] = "promoted"
                hit = True
                break
        if hit:
            storage.save_pending(doc)
    return hit


def remove(observation_id: str) -> bool:
    with storage.exclusive_lock(storage.pending_path()):
        doc = storage.load_pending()
        before = len(doc.get("pending_observations", []))
        doc["pending_observations"] = [
            o for o in doc.get("pending_observations", [])
            if o.get("observation_id") != observation_id
        ]
        changed = len(doc["pending_observations"]) != before
        if changed:
            storage.save_pending(doc)
    return changed
