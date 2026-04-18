"""Append-only audit log."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from passport import storage
from passport.models import Action, AuditEntry, utcnow


def _hash_payload(payload: Any) -> str:
    blob = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _next_audit_id(doc: dict) -> str:
    n = len(doc.get("entries", [])) + 1
    return f"aud_{n:04d}"


def make_entry(
    action: Action,
    actor: str,
    target_claim_id: str | None,
    payload: Any,
    reason: str | None = None,
    replacement_claim_id: str | None = None,
) -> AuditEntry:
    doc = storage.load_audit()
    return AuditEntry(
        audit_id=_next_audit_id(doc),
        timestamp=utcnow(),
        action=action,
        actor=actor,
        target_claim_id=target_claim_id,
        replacement_claim_id=replacement_claim_id,
        reason=reason,
        payload_sha256=_hash_payload(payload),
    )


def append(entry: AuditEntry, commit_sha: str | None = None) -> None:
    with storage.exclusive_lock(storage.audit_path()):
        doc = storage.load_audit()
        entry_dict = entry.model_dump(mode="json")
        if commit_sha:
            entry_dict["commit_sha"] = commit_sha
        doc.setdefault("entries", []).append(entry_dict)
        storage.save_audit(doc)
