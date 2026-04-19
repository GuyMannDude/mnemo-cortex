"""FastAPI router for the five Passport Lane MCP tools.

Endpoints (all under /passport):
    POST /context   → get_user_context
    POST /observe   → observe_behavior
    POST /pending   → list_pending_observations
    POST /promote   → promote_observation
    POST /override  → forget_or_override
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from passport import (
    audit,
    export,
    git_helper,
    pending,
    promotion,
    override as override_mod,
    storage,
    validation,
)
from passport.models import Action, ClaimType, Evidence, Observation


router = APIRouter(prefix="/passport", tags=["passport"])


# ─── Request/response models ────────────────────────────────────────────────

class EvidenceIn(BaseModel):
    evidence_id: Optional[str] = None
    session_id: Optional[str] = None
    turn_ref: str
    excerpt: str = Field(max_length=400)
    # Phase 1.5 — per-row provenance. Clients should supply when known; the
    # API falls back to observation-level source_platform resolution otherwise.
    origin_type: Optional[str] = None
    provenance_bucket: Optional[str] = None
    capture_mode: Optional[str] = None
    origin_uri_hash: Optional[str] = None


class ContextRequest(BaseModel):
    owner_id: Optional[str] = "guy"
    scopes: Optional[list[str]] = None
    include_overlays: bool = True
    platform: Optional[str] = None
    max_claims: int = Field(default=20, ge=1, le=100)


class ContextResponse(BaseModel):
    owner_id: Optional[str]
    passport_version: str
    claims: list[dict]
    overlays: list[dict]
    prompt_block: str


class ObserveRequest(BaseModel):
    owner_id: Optional[str] = "guy"
    proposed_claim: str = Field(max_length=180)
    type: ClaimType = ClaimType.preference
    scope: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.7)
    proposed_target_section: str = "stable_core.communication"
    source_platform: str
    source_session_id: str
    evidence: list[EvidenceIn] = Field(min_length=2)


class ObserveResponse(BaseModel):
    observation_id: Optional[str] = None
    status: Literal["pending", "rejected"]
    rejection_reason: Optional[str] = None
    duplicate_of: Optional[str] = None
    commit_sha: Optional[str] = None
    # Phase 1.5 — Gate 2 classifier output surfaced to the caller.
    disposition: Optional[str] = None        # hard_block | local_only | review_required | allow
    reason_codes: list[str] = Field(default_factory=list)
    flagged_spans: Optional[list[dict]] = None
    evidence_trust: Optional[str] = None
    taint_flags: list[str] = Field(default_factory=list)
    portability: Optional[str] = None
    redacted_claim: Optional[str] = None
    salvageability: Optional[str] = None


class ListPendingRequest(BaseModel):
    owner_id: Optional[str] = "guy"
    status: Optional[Literal["pending", "promoted"]] = "pending"
    limit: Optional[int] = Field(default=25, ge=1, le=200)


class ListPendingResponse(BaseModel):
    items: list[dict]


class PromoteRequest(BaseModel):
    owner_id: Optional[str] = "guy"
    observation_id: str
    target_section: Optional[str] = None
    actor: str = "system"


class PromoteResponse(BaseModel):
    promoted: bool
    claim_id: Optional[str] = None
    target_section: Optional[str] = None
    commit_sha: Optional[str] = None
    reason: Optional[str] = None


class OverrideRequest(BaseModel):
    owner_id: Optional[str] = "guy"
    action: Literal["deprecate", "forget", "override", "replace"]
    target_claim_id: str
    replacement_claim: Optional[str] = None
    reason: Optional[str] = None
    actor: str = "user"


class OverrideResponse(BaseModel):
    success: bool
    action: str
    override_id: Optional[str] = None
    new_claim_id: Optional[str] = None
    commit_sha: Optional[str] = None
    reason: Optional[str] = None


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/context", response_model=ContextResponse)
def get_user_context(req: ContextRequest) -> ContextResponse:
    structured = export.render_structured(
        scopes=req.scopes, platform=req.platform, max_claims=req.max_claims,
    )
    prompt = export.render_prompt_block(
        scopes=req.scopes, platform=req.platform, max_claims=req.max_claims,
    )
    return ContextResponse(
        owner_id=structured.get("owner_id"),
        passport_version=structured.get("passport_version", "0.1"),
        claims=structured.get("claims", []),
        overlays=structured.get("overlays", []) if req.include_overlays else [],
        prompt_block=prompt,
    )


@router.post("/observe", response_model=ObserveResponse)
def observe_behavior(req: ObserveRequest) -> ObserveResponse:
    # Mint evidence ids for any entries that didn't supply one. Provenance
    # fields are carried through; validation.py infers defaults per-row.
    evs: list[dict] = []
    for i, e in enumerate(req.evidence):
        eid = e.evidence_id or f"ev_{req.source_session_id}_{i+1}"
        sid = e.session_id or req.source_session_id
        evs.append(Evidence(
            evidence_id=eid,
            session_id=sid,
            turn_ref=e.turn_ref,
            excerpt=e.excerpt,
            origin_type=e.origin_type,
            provenance_bucket=e.provenance_bucket,
            capture_mode=e.capture_mode,
            origin_uri_hash=e.origin_uri_hash,
        ).model_dump(mode="json"))

    # Build an Observation *without* adding to pending yet, so we can validate first.
    candidate = Observation(
        observation_id="__pending__",
        proposed_claim=req.proposed_claim,
        type=req.type,
        scope=req.scope,
        confidence=req.confidence,
        proposed_target_section=req.proposed_target_section,
        source_platform=req.source_platform,
        source_session_id=req.source_session_id,
        evidence=[Evidence.model_validate(e) for e in evs],
    )
    stable = storage.load_stable()
    vr = validation.validate_observation(candidate, stable)

    # Gate 2 stub — disposition routes the write:
    #   hard_block       → reject, never enters pending
    #   local_only       → accept; promotion to shared scope blocked downstream
    #   review_required  → accept; human sign-off required before promote
    #   allow            → accept as normal
    if vr.disposition == "hard_block":
        return ObserveResponse(
            status="rejected",
            rejection_reason=vr.reason,
            duplicate_of=vr.duplicate_of,
            disposition=vr.disposition,
            reason_codes=vr.reason_codes,
            flagged_spans=vr.flagged_spans,
            evidence_trust=vr.evidence_trust,
            taint_flags=vr.taint_flags,
            portability=vr.portability,
            redacted_claim=vr.redacted_claim,
            salvageability=vr.salvageability,
        )

    obs = pending.add(
        proposed_claim=req.proposed_claim,
        type=req.type.value,
        scope=req.scope,
        confidence=req.confidence,
        proposed_target_section=req.proposed_target_section,
        source_platform=req.source_platform,
        source_session_id=req.source_session_id,
        evidence=evs,
        validation_snapshot=vr.to_snapshot(),
    )

    entry = audit.make_entry(
        Action.observe,
        actor=req.source_platform,
        target_claim_id=obs.observation_id,
        payload=obs.model_dump(mode="json"),
        reason="observation recorded",
    )
    sha = git_helper.commit("observe", obs.observation_id, req.proposed_claim)
    audit.append(entry, commit_sha=sha)

    return ObserveResponse(
        observation_id=obs.observation_id,
        status="pending",
        commit_sha=sha,
        disposition=vr.disposition,
        reason_codes=vr.reason_codes,
        flagged_spans=vr.flagged_spans,
        evidence_trust=vr.evidence_trust,
        taint_flags=vr.taint_flags,
        portability=vr.portability,
        redacted_claim=vr.redacted_claim,
        salvageability=vr.salvageability,
    )


@router.post("/pending", response_model=ListPendingResponse)
def list_pending_observations(req: ListPendingRequest) -> ListPendingResponse:
    items = pending.list_all(status_filter=req.status, limit=req.limit)
    return ListPendingResponse(items=items)


@router.post("/promote", response_model=PromoteResponse)
def promote_observation(req: PromoteRequest) -> PromoteResponse:
    result = promotion.promote(
        observation_id=req.observation_id,
        target_section=req.target_section,
        actor=req.actor,
    )
    if not result.promoted:
        return PromoteResponse(promoted=False, reason=result.reason)
    return PromoteResponse(
        promoted=True,
        claim_id=result.claim_id,
        target_section=result.target_section,
        commit_sha=result.commit_sha,
    )


@router.post("/override", response_model=OverrideResponse)
def forget_or_override(req: OverrideRequest) -> OverrideResponse:
    if req.action == "override" and not req.replacement_claim:
        raise HTTPException(status_code=422, detail="replacement_claim required for action=override")
    result = override_mod.apply(
        action=req.action,
        target_claim_id=req.target_claim_id,
        replacement_claim=req.replacement_claim,
        reason=req.reason,
        actor=req.actor,
    )
    if not result.success:
        return OverrideResponse(success=False, action=result.action, reason=result.reason)
    return OverrideResponse(
        success=True,
        action=result.action,
        override_id=result.override_id,
        new_claim_id=result.new_claim_id,
        commit_sha=result.commit_sha,
    )
