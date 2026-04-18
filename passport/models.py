"""Pydantic models for Passport Lane.

Schema authority: AL's design memo §6 + §7. Phase 1 subset — no overlays,
adapters, or policy models yet.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── Enums ───────────────────────────────────────────────────────────────────

class ClaimType(str, Enum):
    preference = "preference"
    workflow_default = "workflow_default"
    negative_constraint = "negative_constraint"
    style_default = "style_default"
    decision_pattern = "decision_pattern"
    mode_trait = "mode_trait"


class ClaimStatus(str, Enum):
    pending = "pending"
    active = "active"
    deprecated = "deprecated"
    overridden = "overridden"
    forgotten = "forgotten"


class Durability(str, Enum):
    soft = "soft"
    durable = "durable"
    pinned = "pinned"


class Action(str, Enum):
    observe = "observe"
    promote = "promote"
    deprecate = "deprecate"
    forget = "forget"
    override = "override"


# Type-prefix map used by promotion.py to mint claim_ids.
TYPE_PREFIX: dict[ClaimType, str] = {
    ClaimType.preference: "pref",
    ClaimType.workflow_default: "wf",
    ClaimType.negative_constraint: "neg",
    ClaimType.style_default: "style",
    ClaimType.decision_pattern: "dec",
    ClaimType.mode_trait: "mode",
}


# ─── Core objects ────────────────────────────────────────────────────────────

class Evidence(BaseModel):
    evidence_id: str
    session_id: str
    turn_ref: str
    excerpt: str = Field(max_length=400)


class Claim(BaseModel):
    claim_id: str
    claim: str = Field(max_length=180)
    type: ClaimType
    scope: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: ClaimStatus = ClaimStatus.active
    durability: Durability = Durability.durable
    created_at: datetime = Field(default_factory=utcnow)
    last_confirmed_at: Optional[datetime] = None
    source_platforms: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=2)
    tags: list[str] = Field(default_factory=list)
    supersedes: Optional[list[str]] = None


class Observation(BaseModel):
    observation_id: str
    proposed_claim: str = Field(max_length=180)
    type: ClaimType
    scope: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["pending", "promoted"] = "pending"
    proposed_action: Literal["promote"] = "promote"
    proposed_target_section: str
    source_platform: str
    source_session_id: str
    evidence: list[Evidence] = Field(min_length=2)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("proposed_target_section")
    @classmethod
    def _section_shape(cls, v: str) -> str:
        # Must be a dotted path like "stable_core.workflow" or bare "negative_constraints".
        if not v or any(ch.isspace() for ch in v):
            raise ValueError("target_section must be non-empty, no whitespace")
        return v


class AuditEntry(BaseModel):
    audit_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    action: Action
    actor: str
    target_claim_id: Optional[str] = None
    replacement_claim_id: Optional[str] = None
    reason: Optional[str] = None
    payload_sha256: str
    commit_sha: Optional[str] = None
