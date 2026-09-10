"""
Mnemo Cortex v3.0.0 — Drop-in Memory Superhero for AI Agents
=============================================================
Every AI agent has amnesia. Mnemo Cortex is the cure.
Five endpoints. Any LLM. Total recall.

  /health      → System status + provider failover state + session stats
  /context     → Persona-aware L1/L2/L3 + hot session search
  /preflight   → Persona-aware PASS / ENRICH / WARN / BLOCK
                 (UNAVAILABLE when validation itself failed — caller decides)
  /ingest      → Live wire: capture every prompt/response as it happens
  /writeback   → Curated session archiving (still works, complementary)

https://github.com/GuyMannDude/mnemo-cortex
"""

import os
import re
import math
import sqlite3
import json
import time
import hashlib
import hmac
import ipaddress
import logging
import asyncio
import statistics
import uuid
import httpx
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from agentb import __version__
from agentb.config import (
    load_config, AgentBConfig, get_agent_data_dir, get_persona, persona_recall_mode, PersonaConfig,
    validate_agent_id, validate_session_id,
    ExpansionConfig,
)
from agentb.providers import create_resilient_reasoning, create_resilient_embedding
from agentb.cache import l3_scan, ContextChunk
from agentb.fsutil import atomic_write_text as _atomic_write_text
from agentb.ledger import get_ledger, LedgerBroken
from agentb.sessions import SessionManager, SessionConfig
from agentb.provenance import (
    VALID_SOURCES, VALID_CATEGORIES, DEFAULT_HIDDEN_CATEGORIES,
    suggest_category, compute_stale_warning,
)
from agentb.classify import classify_category, reclassify_memory_dir, is_routine_log
from agentb.dedup import find_near_duplicates
from agentb.redact import redact_text, redact_obj
from agentb.capture_gate import CaptureGate
from agentb.ranking import composite_score, pool_similarities, explore_score, order_revisions, exact_first
from agentb.analyst import analyze_tenant, muse_tenant
from agentb.vec import (
    VecStore, VecHit, detect_mode as vec_detect_mode, backfill as vec_backfill,
    VecDimMismatch, EMBED_DIM, LEX_EXACT_MAX_DF, lexical_terms, unit_vector,
)
from agentb.trajectory import TrajectoryStore, embedding_text as traj_embedding_text
from agentb.facts_store import FactsStore, CONFIDENCE_LEVELS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agentb")


# ─────────────────────────────────────────────
#  Request/Response Models
# ─────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str
    reasoning: dict
    embedding: dict
    agents_configured: list[str]
    default_persona: str
    sessions: dict
    # v4.1 — capture pause gate state ({"paused": false} when capturing)
    capture: dict = Field(default_factory=dict)


class IngestRequest(BaseModel):
    prompt: str = Field(..., description="The user's prompt")
    response: str = Field(..., description="The agent's response")
    agent_id: Optional[str] = Field(None, description="Agent ID for tenant isolation")
    metadata: Optional[dict] = Field(None, description="Optional metadata (images, tool calls, etc)")


class IngestResponse(BaseModel):
    status: str
    session_id: str
    entry_number: int
    agent_id: Optional[str]
    # v4.1 — how many secrets were redacted before storage (0 = clean)
    redactions: int = 0


class ContextRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="The prompt to search context for")
    agent_id: Optional[str] = Field(None, description="Agent ID for tenant isolation")
    persona: Optional[str] = Field(None, description="Persona mode: default, strict, creative")
    max_results: int = Field(5, ge=1, le=20)
    mode: Optional[str] = Field(
        None,
        pattern="^(focus|explore|recent)$",
        description=(
            "Recall lens. 'focus': best match wins — similarity + recency + "
            "importance + access. 'explore' (the serendipity lens): what does "
            "this remind the store of — prefers the adjacent similarity band, "
            "ignores recency, favors rarely-recalled memories. 'recent' (the "
            "boot lens): similarity only gates — every on-topic memory is served "
            "newest first. Omitted: the persona decides (context_bias "
            "associative -> explore, else focus; the response's `mode` names "
            "which lens served). Use explore for brainstorming and idea recall; "
            "use recent for 'what was I doing' — a persona's default lens never "
            "answers that question."
        ),
    )
    # v3 provenance + decay filters (all optional)
    source: Optional[str] = Field(
        None,
        description=(
            "Filter to chunks whose provenance source matches: "
            "user|tool|inferred|brain|migrated. Strict — pre-v3 chunks "
            "(no source on record) are dropped when this is set."
        ),
    )
    category: Optional[str] = Field(
        None,
        description=(
            "Filter to chunks whose category matches: topology|current_state|"
            "doctrine|incident|identity|relationship|decision|idea|session_log|unknown."
        ),
    )
    exclude_categories: Optional[list[str]] = Field(
        None,
        description=(
            "Categories to hide. Defaults to DEFAULT_HIDDEN_CATEGORIES "
            "(session_log). Pass an empty list to disable hiding entirely."
        ),
    )
    expand: Optional[bool] = Field(
        None,
        description=(
            "Thesaurus Loop query expansion. None = server default; True = force; "
            "False = never. Expansion only ever fires on a weak/empty first pass "
            "(escalation), so it never slows a good search."
        ),
    )
    batch: bool = Field(
        False,
        description=(
            "Mark a bulk/offline recall (backfill, scripted sweeps). Batch calls "
            "never trigger query expansion — the Thesaurus Loop is live-path only."
        ),
    )
    exclude_stale: bool = Field(
        False,
        description="If True, drop chunks whose stale_warning.severity == 'stale'.",
    )
    max_age_days: Optional[int] = Field(
        None, description="Drop chunks older than N days. None = no age cap."
    )


    # v4.17.1: an empty or whitespace-only prompt embedded fine and came back
    # with a "memory" (explore mode served one chunk for ""). There is nothing
    # to recall against; refuse at the door like every other request model.
    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must not be empty or whitespace-only")
        return v

class ContextChunkResponse(BaseModel):
    content: str
    source: str
    relevance: float
    cache_tier: str
    # v4.1 — lets callers/tools reference the exact memory (dedup sweeps,
    # access tooling) without parsing it out of `source`
    memory_id: Optional[str] = None
    # v3 fields — surfaced when the chunk carries them
    provenance_source: Optional[str] = None
    category: Optional[str] = None
    additional_tags: list[str] = []
    age_days: Optional[float] = None
    stale_warning: Optional[dict] = None


class ContextResponse(BaseModel):
    chunks: list[ContextChunkResponse]
    total_found: int
    latency_ms: float
    cache_hits: dict
    # v4.17 — which lens served: the request's mode, or the persona's when omitted
    mode: str = "focus"
    agent_id: Optional[str]
    persona: str
    provider_used: str


class ChatTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=8000)


class ChatRequest(BaseModel):
    """One Pocket Mnemo turn: recall -> reason -> save, inside one tenant."""
    message: str = Field(..., min_length=1, max_length=8000,
                         description="What the person said")
    agent_id: Optional[str] = Field(
        None, description="Memory tenant. A scoped token may omit it — the pin decides.")
    session_id: Optional[str] = Field(
        None, description="The phone's session (a new one per lid-open). Server-minted if omitted.")
    persona: Optional[str] = Field(None, description="Persona override: default, strict, creative")
    history: list[ChatTurn] = Field(
        default_factory=list, max_length=20,
        description="Recent turns of this conversation (the phone keeps the transcript)")
    save: bool = Field(True, description="Save this turn as a memory (source=user, tag pocket)")
    max_memories: int = Field(6, ge=0, le=20, description="How many memories to recall; 0 = none")

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty or whitespace-only")
        return v


class ChatMemory(BaseModel):
    memory_id: Optional[str] = None
    content: str
    category: Optional[str] = None
    age_days: Optional[float] = None
    relevance: float


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    agent_id: Optional[str]
    persona: str
    # the "what I remember about you" pane — exactly what the reply was given
    memories: list[ChatMemory]
    # archived | held (near-duplicate) | paused (capture gate) | skipped | failed
    save_status: str
    memory_id: str = ""
    latency_ms: float
    provider_used: str


class PreflightRequest(BaseModel):
    prompt: str = Field(..., description="The user's original prompt")
    draft_response: str = Field(..., description="The agent's draft response")
    agent_id: Optional[str] = Field(None)
    persona: Optional[str] = Field(None, description="Persona mode override")


class PreflightResponse(BaseModel):
    verdict: str
    confidence: float
    reason: str
    enrichment: Optional[str] = None
    latency_ms: float
    persona: str
    provider_used: str


class WritebackRequest(BaseModel):
    session_id: str
    summary: str
    key_facts: list[str] = []
    projects_referenced: list[str] = []
    decisions_made: list[str] = []
    agent_id: Optional[str] = None
    timestamp: Optional[str] = None
    # v3 provenance + decay (all optional; safe defaults applied server-side)
    source: Optional[str] = Field(
        None,
        description=(
            "Where this fact came from: user|tool|inferred|brain|migrated. "
            "Defaults to 'inferred' if omitted or invalid."
        ),
    )
    category: Optional[str] = Field(
        None,
        description=(
            "Category that drives decay: topology|current_state|doctrine|incident|"
            "identity|relationship|decision|idea|session_log|unknown. If omitted, "
            "the regex auto-suggester runs against summary + key_facts."
        ),
    )
    additional_tags: list[str] = Field(
        default_factory=list, description="Free-form human-readable tags."
    )
    batch: bool = Field(
        False,
        description=(
            "Set True for bulk/offline writers (e.g. the nightly dreamer) so "
            "embedding bypasses the live circuit breaker — a large batch must "
            "not trip or be blocked by the breaker that guards live /context."
        ),
    )
    force: bool = Field(False, description="Save despite confirmed near-duplicates.")
    supersedes: list[str] = Field(
        default_factory=list,
        description="Save and demote these existing memory ids.")


class WritebackResponse(BaseModel):
    status: str
    memory_id: str
    agent_id: Optional[str]
    l1_bundles_updated: int
    message: str
    # v3 — what the server actually stored + what the regex suggested
    category_used: Optional[str] = None
    category_suggested: Optional[str] = None
    category_match_keywords: Optional[list[str]] = None
    source_used: Optional[str] = None
    # v4.1 — how many secrets were redacted before storage (0 = clean)
    redactions: int = 0
    near_duplicates: list[dict] = Field(default_factory=list)


# ── v4.5 Trajectory Learning ──

class TrajectoryStep(BaseModel):
    action: str = Field(..., description="What the agent did at this step")
    tool_used: Optional[str] = Field(None, description="Tool/command used, if any")
    args: Optional[dict] = Field(None, description="Arguments passed, if any")
    result_summary: str = Field("", description="What the step produced")


class TrajectorySaveRequest(BaseModel):
    agent_id: Optional[str] = Field(None, description="Who completed the task")
    task_type: str = Field(..., min_length=1, max_length=128,
                           description="Category tag, e.g. shopify_fix, bus_debug")
    task_description: str = Field(..., min_length=1, max_length=10000,
                                 description="The goal of the task")
    steps: list[TrajectoryStep] = Field(..., description="Ordered steps taken")
    outcome: str = Field(..., min_length=1, max_length=10000,
                        description="Final result of the task")
    rating: int = Field(..., ge=1, le=5, description="Agent self-assessment 1–5")
    token_cost: Optional[int] = Field(None, ge=0)
    model: Optional[str] = None
    duration_seconds: Optional[int] = Field(None, ge=0)
    # v4.7 provenance (Dreamer Stage 0.7 distillation). Hand-saved Phase-1
    # recipes omit all three: source defaults to "agent", derived_from stays
    # None (a hand-saved recipe is implicitly a success).
    derived_from: Optional[Literal["success", "failure"]] = Field(
        None, description="For distilled strategies: lesson from a success or a failure")
    source: Literal["agent", "dreamer"] = Field(
        "agent", description="agent = hand-saved recipe; dreamer = Stage 0.7 distilled")
    evidence_source: Optional[str] = Field(
        None, max_length=500, description="Where the lesson was observed, e.g. session id + date")


class TrajectorySaveResponse(BaseModel):
    status: str
    trajectory_id: str
    agent_id: Optional[str]
    task_type: str
    total_for_agent: int
    message: str


class TrajectoryRecallRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=10000,
                      description="NL description of what's about to be done")
    agent_id: Optional[str] = Field(None, description="Whose trajectories to search")
    task_type: Optional[str] = Field(None, description="Filter by category tag")
    min_rating: int = Field(3, ge=1, le=5, description="Quality threshold")
    max_results: int = Field(3, ge=1, le=20)


class TrajectoryRecallResponse(BaseModel):
    trajectories: list[dict]
    total_found: int
    agent_id: Optional[str]


# ─────────────────────────────────────────────
#  Tenant Manager — isolated cache/memory per agent
# ─────────────────────────────────────────────

class TenantManager:
    """Manages isolated L1/L2 caches, memory dirs, and session managers per agent_id."""

    def __init__(self, config: AgentBConfig):
        self.config = config
        self._tenants: dict[str, dict] = {}

    def get(self, agent_id: Optional[str] = None) -> dict:
        """Get or create isolated cache/memory/sessions for an agent."""
        if agent_id is not None:
            try:
                validate_agent_id(agent_id)
            except ValueError as e:
                raise HTTPException(400, str(e))
        key = agent_id or "default"
        if key in self._tenants:
            return self._tenants[key]

        data_dir = get_agent_data_dir(self.config, agent_id)
        memory_dir = data_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        # Session config from agent settings or defaults
        session_cfg = SessionConfig()
        if agent_id and agent_id in self.config.agents:
            # Could extend AgentConfig with session settings later
            pass

        vec_mode = vec_detect_mode(memory_dir)
        vec_store = VecStore(data_dir / "vec_index.sqlite")
        log.info(f"Tenant '{key}' vec index ({vec_mode} mode, {vec_store.count()} embedded)")

        # v4.5: trajectory learning — proven task recipes, isolated under the
        # tenant's own trajectories/ dir (JSONL truth + its own vec index).
        traj_store = TrajectoryStore(data_dir / "trajectories")

        tenant = {
            "data_dir": data_dir,
            "memory_dir": memory_dir,
            "sessions": SessionManager(data_dir, session_cfg),
            "vec": vec_store,
            "vec_mode": vec_mode,
            "trajectories": traj_store,
        }
        self._tenants[key] = tenant
        log.info(f"Tenant '{key}' initialized at {data_dir}")
        return tenant

    def has_named_tenants(self) -> bool:
        """Whether this installation contains any non-default tenant.

        A missing ``agent_id`` retains the historical default-tenant behavior
        for genuinely single-tenant installs.  Once named tenants exist,
        however, silently querying the empty ``default`` tenant is ambiguous
        and dangerous: callers can mistake a green zero-result response for an
        empty shared store (#1941).
        """
        if self.config.agents:
            return True
        agents_dir = Path(self.config.data_dir) / "agents"
        try:
            return any(p.is_dir() and p.name != "default" for p in agents_dir.iterdir())
        except FileNotFoundError:
            return False

    @property
    def active_tenants(self) -> list[str]:
        return list(self._tenants.keys())

    def close(self) -> None:
        """Release persistent SQLite handles owned by initialized tenants."""
        for tenant in self._tenants.values():
            tenant["trajectories"].close()
            tenant["vec"].close()


# ─────────────────────────────────────────────
#  Body-size guard (DoS)
# ─────────────────────────────────────────────

class _BodyTooLarge(HTTPException):
    """Raised mid-stream by BodySizeLimitMiddleware once the byte count
    crosses the cap. An HTTPException subclass on purpose: FastAPI's body
    reader wraps any other exception into a generic 400, but re-raises
    HTTPExceptions untouched — this way the client sees the real 413."""

    def __init__(self, max_bytes: int):
        super().__init__(413, f"Request body too large (limit {max_bytes} bytes)")


class BodySizeLimitMiddleware:
    """Enforce the request-body cap even when Content-Length is absent.

    The old header-only check was bypassable with chunked transfer encoding:
    no Content-Length header → no check → an arbitrarily large body reached
    the JSON parser, the embedder, and disk. Pure ASGI so the count happens
    as the body streams: the honest-header fast path still rejects before
    reading anything, and a chunked body is cut off at the first chunk that
    crosses the cap.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await self._reject(send, 400, b"Invalid Content-Length")
                    return
                if declared > self.max_bytes:
                    await self._reject(send, 413, self._limit_msg())
                    return

        received = 0
        response_started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge(self.max_bytes)
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            # If the app already started responding there is nothing safe to
            # send; either way the body stops being read here.
            if not response_started:
                await self._reject(send, 413, self._limit_msg())

    def _limit_msg(self) -> bytes:
        return f"Request body too large (limit {self.max_bytes} bytes)".encode()

    @staticmethod
    async def _reject(send, status: int, body: bytes):
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


def _log_maintenance_exit(task: "asyncio.Task") -> None:
    """Done-callback for the maintenance task: a silent death here used to
    stop archival/dreamer/Analyst/Muse until the next restart with no trace."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"Maintenance loop DIED: {exc!r} — background archival, "
                  f"dreamer, Analyst and Muse passes are stopped until restart")


# ─────────────────────────────────────────────
#  Preflight System Prompts
# ─────────────────────────────────────────────

BASE_PREFLIGHT_PROMPT = """You are AgentB, a memory coprocessor for AI agents.
Review the agent's draft response against the user's prompt and any memory context.

Respond with EXACTLY this JSON format (no markdown, no backticks):
{{
    "verdict": "PASS|ENRICH|WARN|BLOCK",
    "confidence": 0.0-1.0,
    "reason": "brief explanation",
    "enrichment": "additional context if ENRICH, otherwise null"
}}

Verdicts:
- PASS: Accurate and complete.
- ENRICH: Correct but could be improved with context you have.
- WARN: May contain inaccuracies. Flag for review.
- BLOCK: Contains a clear factual error."""


def build_preflight_system_prompt(persona: PersonaConfig) -> str:
    prompt = BASE_PREFLIGHT_PROMPT
    if persona.custom_system_prompt:
        prompt += f"\n\nADDITIONAL INSTRUCTIONS ({persona.name.upper()} MODE):\n{persona.custom_system_prompt}"
    if persona.preflight == "aggressive":
        prompt += "\n\nYou are in AGGRESSIVE validation mode. Set a HIGH bar for PASS."
    elif persona.preflight == "permissive":
        prompt += "\n\nYou are in PERMISSIVE mode. Only flag clear errors, not speculation."
    return prompt


# ─────────────────────────────────────────────
#  App Factory
# ─────────────────────────────────────────────

# ── Thesaurus Loop: query expansion (v4.2) ──────────────────────────────────
# Fired only on a weak/empty first recall (escalation). One isolated Flash call
# generates alternative phrasings; each is searched and the passes are fused by
# max-relevance. Kept entirely off the shared reasoner breaker — a Flash hiccup
# here must never poison preflight/classification — with its own tight timeout
# and a small LRU so repeat recalls (agent_startup, etc.) cost nothing.

_EXPAND_CACHE: "OrderedDict[str, list[str]]" = OrderedDict()
_EXPAND_CACHE_MAX = 256

_EXPAND_SYSTEM = (
    "You rewrite a search query into alternative phrasings to improve memory "
    "retrieval. Given one query, output up to {n} alternative phrasings that mean "
    "the same thing using DIFFERENT words and synonyms. One phrasing per line. "
    "No numbering, no quotes, no preamble, no commentary — only the phrasings."
)


def _resolve_openrouter_creds(reasoning_cfg) -> tuple[str, str]:
    """Find an already-configured OpenRouter key/base in the reasoning provider
    chain, so expansion reuses existing credentials with zero new config."""
    providers = [reasoning_cfg.primary, *reasoning_cfg.fallbacks]
    for p in providers:
        if getattr(p, "provider", "") == "openrouter" and getattr(p, "api_key", ""):
            return p.api_key, (p.api_base or "https://openrouter.ai/api/v1")
    return "", "https://openrouter.ai/api/v1"


def merge_passes(passes) -> list:
    """Fuse one or more retrieval passes into a single candidate pool.

    THE CRUX of the Thesaurus Loop: when the same memory_id surfaces from more
    than one phrasing, keep the instance with the HIGHEST relevance (RAG-Fusion
    max-relevance), not the first one seen. A plain dict update keeps the
    original insertion position, so a single pass — already tier-deduped inside
    _retrieve_for_embedding — returns byte-identical order to the pre-expansion
    handler. Chunks without a memory_id dedup by (source, content) — /context
    no longer produces any (the HOT tier was retired in E3), the fallback
    stays for callers that do.
    """
    merged: dict = {}
    for pass_chunks in passes:
        for c in pass_chunks:
            key = c.memory_id or ("__hot__", c.source, c.content)
            prev = merged.get(key)
            if prev is None or c.relevance > prev.relevance:
                merged[key] = c
    return list(merged.values())


def chunk_age_days(c, now: float) -> float:
    """Age of a pooled chunk in days; unknown age sorts as oldest."""
    if c.age_days is not None:
        return float(c.age_days)
    if c.created_at:
        return (now - float(c.created_at)) / 86400.0
    return float("inf")


def top_relevance(chunks) -> float:
    """Highest raw relevance in a candidate pool (0.0 if empty). Raw, not the
    composite score — the escalation check runs before the re-rank."""
    return max((c.relevance for c in chunks), default=0.0)


def median_relevance(chunks) -> float:
    """Median raw relevance in a candidate pool (0.0 if empty). Paired with
    top_relevance to measure how far the best hit rises above the pack — the
    embedder-agnostic 'shape' signal the escalation uses."""
    rels = [c.relevance for c in chunks]
    return statistics.median(rels) if rels else 0.0


def should_expand(prompt: str, chunks, cfg: ExpansionConfig) -> bool:
    """Escalate to expansion only when the first pass whiffed.

    Too-short queries (likely single-entity lookups) never expand. Otherwise
    expand when the pass is EMPTY, or when its distribution is FLAT — the top hit
    barely rises above the median (top - median < gap_threshold), meaning nothing
    stood out and a rephrase is worth one Flash call.

    The trigger is the relative top-vs-pack gap, NOT an absolute relevance floor:
    this embedder compresses scores into a narrow band where good recalls and
    noise overlap (the v4.3.0 absolute floor sat inside that band and never
    fired), but a strong recall still PEAKS above its own pack regardless of where
    the band sits. A uniform pool (incl. a single result, where top == median)
    expands — the accepted, near-free false-positive per the locked design."""
    if len(prompt.split()) < cfg.min_query_words:
        return False
    if not chunks:
        return True
    return top_relevance(chunks) - median_relevance(chunks) < cfg.gap_threshold


async def expand_query(prompt: str, cfg: ExpansionConfig, api_key: str, api_base: str) -> list[str]:
    """Return up to cfg.max_variants alternative phrasings, or [] on any failure.
    Isolated: own httpx client + hard timeout, no breaker. Non-empty results are
    LRU-cached by normalized query; failures are never cached (could be transient)."""
    if not api_key or cfg.max_variants < 1:
        return []
    key = prompt.strip().lower()
    cached = _EXPAND_CACHE.get(key)
    if cached is not None:
        _EXPAND_CACHE.move_to_end(key)
        return list(cached)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/GuyMannDude/mnemo-cortex",
        "X-Title": "Mnemo Cortex",
    }
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _EXPAND_SYSTEM.format(n=cfg.max_variants)},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 160,
        "temperature": 0.7,
    }
    base = api_base or "https://openrouter.ai/api/v1"
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_ms / 1000.0) as client:
            resp = await client.post(f"{base}/chat/completions", headers=headers, json=body)
            resp.raise_for_status()
            # `content` can be null on a refused/tool-only completion — coerce to
            # "" INSIDE the guard so the parse loop never sees None (a None here
            # would AttributeError past this try and 500 the live recall path).
            text = resp.json()["choices"][0]["message"].get("content") or ""
    except Exception as e:
        # type name matters: a bare timeout str()s to empty, hiding the cause.
        log.warning(f"query expansion call failed (no expansion this query): "
                    f"{type(e).__name__}: {e}")
        return []

    original = prompt.strip().lower()
    variants: list[str] = []
    for line in text.splitlines():
        # Strip a leading list marker only — a bullet (-, •, *) or an ordered
        # marker (digits + . or )). NOT a char-set lstrip: that would eat the
        # leading digits of a real phrasing ("3D printing" → "D printing").
        v = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s+", "", line).strip().strip('"')
        if v and v.lower() != original and v.lower() not in (x.lower() for x in variants):
            variants.append(v)
        if len(variants) >= cfg.max_variants:
            break

    if variants:
        _EXPAND_CACHE[key] = list(variants)
        _EXPAND_CACHE.move_to_end(key)
        while len(_EXPAND_CACHE) > _EXPAND_CACHE_MAX:
            _EXPAND_CACHE.popitem(last=False)
    return variants


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def auth_posture_is_open(config: AgentBConfig) -> bool:
    """True if this config would expose endpoints on a non-loopback interface
    with no auth configured and no explicit opt-in. The dangerous case."""
    has_auth = bool(config.server.auth_token or config.server.scoped_tokens)
    if has_auth or config.server.allow_unauthenticated:
        return False
    return not _is_loopback_host(config.server.host)


def assert_safe_auth_posture(config: AgentBConfig) -> None:
    """Fail closed at the bind path: refuse to serve a non-loopback interface
    with no auth. Raises RuntimeError with remediation steps."""
    if auth_posture_is_open(config):
        raise RuntimeError(
            f"Refusing to start: host={config.server.host!r} exposes every "
            f"endpoint (including /writeback and /capture/pause) with NO auth "
            f"configured. Fix one of:\n"
            f"  • set server.auth_token (or server.scoped_tokens)\n"
            f"  • set server.host: 127.0.0.1  (loopback only)\n"
            f"  • set server.allow_unauthenticated: true  (deliberate open deploy "
            f"behind an external gatekeeper)"
        )


def _epoch_from_iso(value: str) -> Optional[float]:
    """ISO-8601 → epoch seconds; naive stamps are read as UTC. Returns None
    when the value is not ISO-8601 — the caller decides the fallback and
    makes it visible. Never raises: a bad clock must never block a write."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ── Pocket Mnemo (v4.19) — the phone is the door, this server is the engine ──
POCKET_APP_DIR = Path(__file__).parent / "app"
# Closed allowlist: the only files /app/ will ever serve. No directory walk.
POCKET_APP_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon.svg": "image/svg+xml",
}
POCKET_MAX_TOKENS = 700          # a phone screen, not an essay
POCKET_MEMORY_CHARS = 600        # per recalled memory, in the prompt
POCKET_REPLY_IN_MEMORY = 300     # how much of the reply rides into the saved turn
POCKET_SYSTEM = (
    "You are Pocket Mnemo: a small assistant living in someone's pocket whose one "
    "superpower is memory. The memories below are things this person told you "
    "before; a line marked 'Pocket replied' is what YOU said back at the time — "
    "trust the person's words, not your old replies. Use memories when they "
    "matter. If you don't remember something, say so plainly — never invent a "
    "memory. When they tell you something new, say you'll remember it. Talk like "
    "a sharp friend: plain words, short answers that fit a phone screen, no "
    "corporate voice, no lectures."
)


def _is_pocket_app_path(path: str) -> bool:
    return path == "/app" or path.startswith("/app/")


def _pocket_prompt(memories: list, history: list, message: str) -> str:
    lines = []
    if memories:
        lines.append("What you remember about this person (most relevant first):")
        for m in memories:
            if m.age_days is None:
                age = "unknown age"
            else:
                age = "today" if m.age_days < 1 else f"{int(m.age_days)}d ago"
            lines.append(f"- ({age}) {m.content}")
    else:
        lines.append("You have no memories of this person yet.")
    if history:
        lines.append("")
        lines.append("Conversation so far:")
        for t in history:
            lines.append(("User: " if t.role == "user" else "You: ") + t.content)
    lines.append("")
    lines.append(f"User: {message}")
    lines.append("You:")
    return "\n".join(lines)


def create_app(config: Optional[AgentBConfig] = None) -> FastAPI:
    if config is None:
        config = load_config()

    log.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    if auth_posture_is_open(config):
        log.warning(
            "SECURITY: serving host=%s with no auth configured — every endpoint "
            "is world-writable. Set server.auth_token, bind 127.0.0.1, or set "
            "server.allow_unauthenticated: true to silence this.",
            config.server.host,
        )

    reasoner = create_resilient_reasoning(config.reasoning)
    embedder = create_resilient_embedding(config.embedding)
    tenants = TenantManager(config)

    # Thesaurus Loop credentials: reuse whatever OpenRouter key the reasoning
    # chain already carries (zero new config), unless expansion overrides it.
    expand_or_key, expand_or_base = _resolve_openrouter_creds(config.reasoning)
    if config.expansion.api_key:
        expand_or_key = config.expansion.api_key
    if config.expansion.api_base:
        expand_or_base = config.expansion.api_base

    # Phase 3: shared global facts store (one file, all agents share)
    data_root = Path(config.data_dir or os.path.expanduser("~/.agentb"))
    facts_path = data_root / "facts.sqlite"
    facts = FactsStore(facts_path)

    # v4.1: capture pause gate — server-wide, file-backed, auto-resuming.
    gate = CaptureGate(data_root)

    # Pre-initialize configured agents
    for agent_name in config.agents:
        tenants.get(agent_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Fail closed on EVERY serving path — this runs under uvicorn/gunicorn
        # (e.g. `uvicorn agentb.server:create_app --factory`) as well as
        # `python -m agentb.server`. Refuses to serve a non-loopback bind with
        # no auth unless server.allow_unauthenticated is set.
        assert_safe_auth_posture(config)
        log.info(f"⚡ Mnemo Cortex v{__version__} — I remember everything so your agent doesn't have to.")
        log.info(f"  Reasoning: {reasoner.status}")
        log.info(f"  Embedding: {embedder.status}")
        log.info(f"  Data dir:  {config.data_dir}")
        log.info(f"  Agents:    {list(config.agents.keys()) or ['default']}")
        log.info(f"  Personas:  {list(config.personas.keys())}")
        log.info(f"  Live Wire: /ingest endpoint active — every exchange captured")
        # Keep a reference (a bare create_task can be garbage-collected) and
        # log loudly if the loop ever exits — background archival, the
        # dreamer-reclassify, Analyst, and Muse passes all die with it.
        maintenance_task = asyncio.create_task(maintenance_loop())
        maintenance_task.add_done_callback(_log_maintenance_exit)
        try:
            yield
        finally:
            maintenance_task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await maintenance_task
            finally:
                tenants.close()

    app = FastAPI(
        title="Mnemo Cortex",
        description="Drop-in memory superhero for AI agents",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=config.server.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

    # The startup guard above checks the configured bind address, but ASGI
    # launchers can override it (for example: a loopback config launched with
    # ``uvicorn agentb.server:create_app --factory --host 0.0.0.0``). Preserve the fail-closed
    # guarantee at request time too: an unauthenticated deployment that has
    # not explicitly opted open may serve loopback clients only, regardless
    # of how the process was actually bound.
    if not (config.server.auth_token or config.server.scoped_tokens or
            config.server.allow_unauthenticated):
        @app.middleware("http")
        async def enforce_unauthenticated_loopback(request: Request, call_next):
            client_host = request.client.host if request.client else ""
            if not _is_loopback_host(client_host):
                return Response(
                    "Forbidden: unauthenticated access is loopback-only",
                    status_code=403,
                )
            return await call_next(request)

    # ── Body-size guard (DoS) ──
    # Reject oversized payloads before they get embedded, indexed, or written
    # to disk. Enforced while the body streams — a header-only check was
    # bypassable by omitting Content-Length (chunked transfer encoding).
    if config.server.max_body_bytes > 0:
        app.add_middleware(BodySizeLimitMiddleware,
                           max_bytes=config.server.max_body_bytes)

    # ── Auth ──
    # Two tiers (v4.9): the master auth_token keeps full access; scoped tokens
    # (server.scoped_tokens) are pinned to one agent tenant + an endpoint
    # allowlist. The middleware decides WHO the token is; the scoped endpoints
    # enforce the tenant pin on the request body via _enforce_scope() — the
    # config loader guarantees only pin-enforcing endpoints can be allowlisted.
    def _token_eq(a: str, b: str) -> bool:
        return hmac.compare_digest(a.encode(), b.encode())

    if config.server.auth_token or config.server.scoped_tokens:
        @app.middleware("http")
        async def check_auth(request: Request, call_next):
            # /app/* is the Pocket Mnemo shell: static, secret-free — the
            # token lives on the phone and rides every /chat call.
            if request.url.path == "/health" or _is_pocket_app_path(request.url.path):
                return await call_next(request)
            token = (request.headers.get("X-API-KEY") or
                     request.headers.get("Authorization", "").replace("Bearer ", ""))
            if config.server.auth_token and _token_eq(token, config.server.auth_token):
                return await call_next(request)
            for st in config.server.scoped_tokens:
                if _token_eq(token, st.token):
                    if request.url.path not in st.endpoints:
                        return Response(
                            f"Forbidden: token for agent '{st.agent_id}' is not "
                            f"scoped to {request.url.path}", status_code=403)
                    request.state.scoped_agent_id = st.agent_id
                    return await call_next(request)
            return Response("Unauthorized", status_code=401)

    def _enforce_scope(request: Request, agent_id: Optional[str]) -> None:
        """403 unless the request's agent_id matches the token's pin. A missing
        agent_id also fails — it would otherwise land in the 'default' tenant."""
        pinned = getattr(request.state, "scoped_agent_id", None)
        if pinned is not None and agent_id != pinned:
            raise HTTPException(
                403, f"Token is scoped to agent '{pinned}'")

    # ── Health ──
    @app.get("/health", response_model=HealthResponse)
    async def health():
        r_ok = await reasoner.health_check()
        e_ok = await embedder.health_check()

        # Aggregate session stats across all tenants
        total_sessions = {"hot": 0, "warm": 0, "cold": 0}
        for t in tenants._tenants.values():
            s = t["sessions"].stats
            total_sessions["hot"] += s["hot_sessions"]
            total_sessions["warm"] += s["warm_sessions"]
            total_sessions["cold"] += s["cold_sessions"]

        return HealthResponse(
            status="ok" if (r_ok and e_ok) else ("degraded" if (r_ok or e_ok) else "down"),
            version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning={**reasoner.status, "healthy": r_ok},
            embedding={**embedder.status, "healthy": e_ok},
            agents_configured=list(config.agents.keys()) + tenants.active_tenants,
            default_persona=config.default_persona,
            sessions=total_sessions,
            capture=gate.status(),
        )

    # ── Context ──
    @app.post("/context", response_model=ContextResponse)
    async def context(req: ContextRequest, request: Request):
        """Recall one tenant's context.

        ``agent_id`` may be omitted only on a legacy/default-only install.  On
        a multi-tenant installation omission is an explicit 400: the server
        will neither invent a tenant nor return a misleading empty 200.  A
        future cross-tenant search must be a separate, deliberate contract.
        """
        _enforce_scope(request, req.agent_id)
        if req.agent_id is None and tenants.has_named_tenants():
            raise HTTPException(
                400,
                "agent_id is required on a multi-tenant installation; "
                "unscoped /context search is not supported",
            )
        start = time.time()
        persona = get_persona(config, req.persona, req.agent_id)
        # v4.17: the lens is the caller's if named, else the persona's
        # (strict/default -> focus, creative -> explore). Reported back as `mode`.
        mode = req.mode or persona_recall_mode(persona)
        tenant = tenants.get(req.agent_id)
        memory_dir = tenant["memory_dir"]
        vec_store: VecStore = tenant["vec"]

        # v3 filter setup. exclude_categories defaults to DEFAULT_HIDDEN_CATEGORIES
        # (session_log). Caller can opt back in by passing an explicit list — even
        # an empty one — to disable hiding.
        # v4.18.4: the `recent` lens exists for "what was I doing", which
        # lives in session_log — hiding it by default would make the lens
        # answer everything except its own question (review, 2026-09-05).
        # recent hides nothing unless the caller says so.
        if req.exclude_categories is None:
            effective_exclude = set() if mode == "recent" else set(DEFAULT_HIDDEN_CATEGORIES)
        else:
            effective_exclude = set(req.exclude_categories)
        # If caller asked for a specific category, never hide it.
        if req.category:
            effective_exclude.discard(req.category)

        # Single source of truth for the metadata filter. Every check is
        # metadata-only (no embedding needed), so the same predicate gates both
        # post-recall chunks (keep_chunk) and the L3 disk-walk *before* it
        # embeds (l3_scan prefilter) — that pushdown is the v3.3.1 perf fix.
        def passes_metadata(source=None, category=None, age_days=None, stale_warning=None) -> bool:
            if req.source:
                # Strict: pre-v3 records have no source, can't satisfy a source filter.
                if not source or source != req.source:
                    return False
            if req.category and category != req.category:
                return False
            if category and category in effective_exclude:
                return False
            if req.max_age_days is not None and age_days is not None and age_days > req.max_age_days:
                return False
            if req.exclude_stale and stale_warning and stale_warning.get("severity") == "stale":
                return False
            return True

        def keep_chunk(c: ContextChunk) -> bool:
            return passes_metadata(
                source=c.provenance_source,
                category=c.category,
                age_days=c.age_days,
                stale_warning=c.stale_warning,
            )

        # Over-fetch so post-filter trims don't leave us short. Explore mode
        # fetches wider: its candidates live below the top hit, so a narrow
        # pool would leave the adjacent band empty.
        pool_factor = 5 if mode == "explore" else 3
        overfetch = max(req.max_results * pool_factor, req.max_results + 5)

        cache_hits = {"HOT": 0, "L1": 0, "VEC": 0, "LEX": 0, "L2": 0, "L3": 0, "MEM0": 0}
        # v4.21: the lexical lane's MATCH expression, built once from the
        # caller's prompt (expansion variants are paraphrases; the exact
        # identifiers the lane exists for live in the original wording).
        lex_terms = lexical_terms(req.prompt) if config.ranking.lexical_enabled else []

        # v4.1: tiers no longer fill a sequential budget. Each tier contributes
        # its filtered candidates to a pool; the pool is re-ranked by the
        # composite score (similarity + recency + category importance + access
        # frequency) and trimmed once at the end. Under the old budget fill,
        # whichever tier ran first owned the result slots regardless of how
        # weak its matches were.
        #
        # v4.2 (Thesaurus Loop): a single retrieval pass is factored out here so
        # the same pass can run once for the original query or once per expanded
        # phrasing. With expansion off the handler calls this exactly once and
        # merge_passes is an identity over that one pass → behaviour is
        # byte-identical to the v4.1 pooled re-rank handler.
        async def _retrieve_for_embedding(query_embedding) -> list[ContextChunk]:
            pass_chunks: list[ContextChunk] = []

            # E3 (2026-09): the HOT, L1 and L2 tiers no longer feed this pool.
            # Measured before the cut (read-only probes, 2026-09-01/02, all
            # four tenants, 366 queries / 3,665 served chunks — served, i.e.
            # post-trim, so a lower bound on what the tiers pooled): VEC
            # 99.8 %, L1 0.2 % (one tenant), HOT and L2 zero everywhere. HOT
            # could only ever serve session_log rows (hidden by default) and
            # the one tenant with hot sessions held heartbeat polls; L2 stopped
            # taking writes in v4.1. Each removed tier also carried its own
            # relevance scale (HOT a 0.75 sentinel, L1 cosine ≥0.75) into a
            # pool whose VEC hits top out near 0.58; the mixing is now down to
            # VEC and L3 (L3 emits raw cosine — both modes normalise the pool
            # via pool_similarities since E4). The cache_hits keys stay for
            # wire compatibility; they report zero.

            # Intra-pass cross-tier dedup: a memory written via /writeback is
            # in the vec index AND on disk where the L3 walk finds it. Without
            # this, the same chunk appears once per tier within this pass. (Cross-PASS
            # dedup — fusing the original query with expanded phrasings — happens
            # in merge_passes, by max-relevance, not first-wins.)
            seen_memory_ids: set[str] = set()

            # VEC: indexed sqlite-vec lookup over written memories.
            # #468: push the category filter INTO the kNN. A session_log-dominated
            # store (cc) otherwise hands back an all-hidden top-k → every category
            # filter falls through to the slow L3 disk-walk. With the category
            # column, search over-fetches and filters in-index so VEC fills the
            # budget. keep_chunk below still disk-truths every hit (final authority).
            # Note the over-fetch compounds: top_k here is already `overfetch`
            # (3×max_results, for the post-filter trim), and search multiplies THAT
            # by the multiplier — so the kNN fetches ~15×max_results candidates when
            # a category filter is active. Intentional headroom for thin categories;
            # the kNN cost of a larger k is negligible and the handler trims to
            # max_results regardless.
            vec_filter_active = bool(req.category) or bool(effective_exclude)

            def _chunk_from_vec_hit(hit) -> ContextChunk:
                # vec0 distance is L2 by default; convert to similarity-ish (0..1)
                relevance = 1.0 / (1.0 + hit.distance)
                # v4.0.1: load category/source from the memory's JSON so the
                # metadata filter (session_log exclusion, stale, source) applies
                # to VEC hits too. Without this the VEC tier silently bypassed
                # every category filter — session_log noise crowded recall.
                category = provenance_source = None
                age_days = stale = None
                revises: list = []
                if hit.source_file and os.path.exists(hit.source_file):
                    try:
                        mj = json.loads(Path(hit.source_file).read_text(encoding="utf-8"))
                        category = mj.get("category")
                        provenance_source = mj.get("source")
                        # v4.18.4: the near-duplicates this write was FORCED past
                        # (ranking.order_revisions). `supersedes` targets leave the
                        # index at write time, so they are never in a pool.
                        if mj.get("near_dup_forced"):
                            revises = list(mj.get("near_dup_of") or [])
                    except Exception:
                        pass
                if hit.created_at:
                    age_days = (time.time() - hit.created_at) / 86400.0
                    if category:
                        stale = compute_stale_warning(category, hit.created_at)
                return ContextChunk(
                    content=hit.text,
                    source=f"memory:{hit.memory_id}",
                    relevance=relevance,
                    cache_tier="VEC",
                    memory_id=hit.memory_id,
                    provenance_source=provenance_source,
                    category=category,
                    age_days=age_days,
                    stale_warning=stale,
                    revises=revises,
                )

            if vec_store.count() > 0:
                def _vec_pass(multiplier: int) -> bool:
                    """Run one filtered kNN and ingest survivors. Returns False
                    only on a search error, so the escalation retry can skip a
                    kNN that would just fail (and log) identically again."""
                    try:
                        vec_hits = vec_store.search(
                            query_embedding,
                            top_k=overfetch,
                            include_category=req.category,
                            exclude_categories=effective_exclude,
                            overfetch_multiplier=multiplier,
                        )
                    except VecDimMismatch as e:
                        log.error(f"vec query dim mismatch: {e}")
                        return False
                    for hit in vec_hits:
                        if hit.memory_id in seen_memory_ids:
                            continue
                        c = _chunk_from_vec_hit(hit)
                        if keep_chunk(c):
                            pass_chunks.append(c)
                            seen_memory_ids.add(hit.memory_id)
                    return True

                vec_ok = _vec_pass(config.cache.vec_category_overfetch_multiplier)
                # v4.9.2 escalation: a filtered kNN comes back short when the
                # query's semantic neighborhood is dominated by a hidden
                # category — session-flavored prompts on a session_log-heavy
                # store land ~all their neighbors in the hidden 65%, so even a
                # 15× over-fetch filters down to less than max_results. One
                # wider retry costs milliseconds; the L3 disk-walk it prevents
                # costs tens of seconds on a large store (S120: 23s observed,
                # 6.2k files). Escalate once, then accept what we have.
                if vec_ok and vec_filter_active and len(pass_chunks) < req.max_results:
                    _vec_pass(config.cache.vec_category_overfetch_multiplier * 5)
                # v4.18.4 — the recent lens RETRIEVES by recency too: the kNN
                # pool holds the nearest neighbours, and on a store of
                # thousands of near-identical session logs the newest one is
                # routinely outside it (review, 2026-09-05). Union the newest
                # rows into the pool; the band gate below decides on-topic.
                if mode == "recent":
                    try:
                        newest_hits = vec_store.newest(query_embedding, n=overfetch,
                                                       include_category=req.category or None,
                                                       exclude_categories=effective_exclude)
                    except VecDimMismatch as e:
                        # same failure the kNN leg screams on; never feed
                        # fabricated distances into the pool (that would also
                        # mark VEC as served and skip the L3 escape hatch)
                        log.error(f"vec newest dim mismatch: {e}")
                        newest_hits = []
                    for hit in newest_hits:
                        if hit.memory_id in seen_memory_ids:
                            continue
                        c = _chunk_from_vec_hit(hit)
                        if keep_chunk(c):
                            pass_chunks.append(c)
                            seen_memory_ids.add(hit.memory_id)

            # LEX (v4.21): the lexical lane. BM25 over the same stored text
            # finds the memory that holds the prompt's exact identifier —
            # an advisory id, an error string, a file name, a hash — when
            # the kNN neighbourhood missed it. Two effects on the pool:
            #   1. a hit already pooled by VEC gets its lexical evidence
            #      attached (composite_score's lexical term);
            #   2. a hit VEC did not pool joins as a LEX chunk, scored on
            #      the SAME cosine scale as everything else — its stored
            #      vector against the query, mapped to VEC's wire
            #      relevance 1/(1+d) — so it earns its rank on meaning
            #      plus words, never on words alone.
            # Evidence is normalised to the lane's best hit (1.0), so a
            # prompt of common words yields weak, evenly spread credit.
            # Gated on the query's dimension like the kNN is: on a mismatch
            # the vector lane screams and serves nothing so L3 can rescue
            # the recall; a LEX cosine over a truncating zip would serve
            # fabricated relevance AND fill the pool past the L3 gate
            # (review, 2026-09-10).
            if lex_terms and vec_store.count() > 0 and len(query_embedding) == EMBED_DIM:
                query_unit = unit_vector(query_embedding)
                try:
                    lex_hits = vec_store.lexical_search(
                        lex_terms,
                        top_k=overfetch,
                        include_category=req.category,
                        exclude_categories=effective_exclude,
                        overfetch_multiplier=config.cache.vec_category_overfetch_multiplier,
                    )
                except sqlite3.Error as e:
                    # the vector lane already served; a broken lexical lane
                    # degrades to 4.20 behaviour out loud, never silently
                    log.error(f"lexical lane failed (agent={req.agent_id}): {e}")
                    lex_hits = []
                # 4.21.1: the rare identifiers the prompt named, and the
                # memories that hold them — pinned to the front of the served
                # window (ranking.exact_first, capped there). One bounded
                # single-term seek per exact term; the hits are pooled even
                # when the OR-query's top_k would have cut them (the live
                # case: rank 19 of 15).
                exact_ids: set[str] = set()
                try:
                    for term in vec_store.exact_terms(lex_terms):
                        for eh in vec_store.lexical_search(
                                [term], top_k=LEX_EXACT_MAX_DF,
                                include_category=req.category,
                                exclude_categories=effective_exclude,
                                prune=False):
                            exact_ids.add(eh.memory_id)
                            if eh.memory_id not in {h.memory_id for h in lex_hits}:
                                lex_hits.append(eh)
                except sqlite3.Error as e:
                    log.error(f"lexical exact pass failed (agent={req.agent_id}): {e}")
                if lex_hits:
                    top_lex = lex_hits[0].score or 1.0
                    pooled = {c.memory_id: c for c in pass_chunks if c.memory_id}
                    for lh in lex_hits:
                        evidence = max(0.0, lh.score / top_lex)
                        already = pooled.get(lh.memory_id)
                        if already is not None:
                            already.lexical = max(already.lexical, evidence)
                            already.exact = already.exact or lh.memory_id in exact_ids
                            continue
                        if lh.memory_id in seen_memory_ids:
                            continue
                        stored = vec_store.get_embedding(lh.memory_id)
                        if stored is None or len(stored) != EMBED_DIM:
                            continue  # text row without a (usable) vector: not a served memory
                        cos = sum(a * b for a, b in zip(stored, query_unit))
                        distance = math.sqrt(max(0.0, 2.0 - 2.0 * cos))
                        c = _chunk_from_vec_hit(VecHit(
                            memory_id=lh.memory_id, text=lh.text, distance=distance,
                            source_file=lh.source_file, created_at=lh.created_at,
                            category=lh.category,
                        ))
                        c.cache_tier = "LEX"
                        c.lexical = evidence
                        c.exact = lh.memory_id in exact_ids
                        if keep_chunk(c):
                            pass_chunks.append(c)
                            seen_memory_ids.add(lh.memory_id)

            # L3: the expensive disk-walk (embeds candidates) stays an escape
            # hatch — only runs when VEC couldn't fill the request AND served
            # nothing at all.
            # #468 / v4.9.2 established the short-circuit for the filtered
            # path: when a category filter was pushed into the kNN and VEC
            # served survivors, VEC (plus its escalation retry) already did
            # the over-fetch L3 would duplicate; a partial result beats the
            # multi-second disk-walk (which on a large store exceeds the bridge
            # timeout — the vec search contract says the caller must NOT fall
            # through). E3 made it uniform: with the HOT/L1/L2 tiers gone,
            # nothing pads the pool between VEC and L3 any more, so on the
            # UN-filtered path (exclude_categories=[] and no category) a VEC
            # pass that under-filled by one would have walked the disk —
            # measured in review at 0 → 80 document embeds on a thin store.
            # Gate on a real VEC contribution, NOT just "index non-empty":
            # in the un-backfilled deploy window the category columns are NULL,
            # so include filters match nothing, exclusion filters drop nothing
            # in-index and keep_chunk then drops everything on disk-truth —
            # both leave zero VEC survivors, and there L3 is still the escape
            # hatch that finds the memories. Every VEC chunk that reached
            # pass_chunks already passed keep_chunk, so a survivor here is by
            # definition on-filter.
            vec_served = any(c.cache_tier == "VEC" for c in pass_chunks)
            if len(pass_chunks) < req.max_results and not vec_served:
                l3_results = [
                    c for c in await l3_scan(memory_dir, query_embedding,
                                              # L3 embeds candidate DOCUMENTS, not the query
                                              embed_fn=lambda t: embedder.embed(t, task_type="document"),
                                              threshold=config.cache.l3_similarity_threshold,
                                              top_k=overfetch,
                                              prefilter=passes_metadata,
                                              max_candidates=config.cache.l3_max_candidates)
                    if keep_chunk(c) and (not c.memory_id or c.memory_id not in seen_memory_ids)
                ]
                pass_chunks.extend(l3_results)

            return pass_chunks

        try:
            query_embedding = await embedder.embed(req.prompt, task_type="query")
        except Exception as e:
            raise HTTPException(503, f"Embedding unavailable: {e}")

        # Standard pass — always runs, identical to v4.1.
        standard = await _retrieve_for_embedding(query_embedding)
        all_chunks = merge_passes([standard])

        # Thesaurus Loop (v4.2): escalate ONLY when the first pass whiffed. A
        # zero/weak result fans the query into a few alternative phrasings (one
        # isolated Flash call), searches each, and fuses by max-relevance. Good
        # searches never reach this branch, so there's no hot-path cost.
        exp = config.expansion
        expansion_allowed = req.expand is True or (req.expand is None and exp.enabled)
        if expansion_allowed and not req.batch \
                and should_expand(req.prompt, all_chunks, exp):
            variants = await expand_query(req.prompt, exp, expand_or_key, expand_or_base)
            extra_passes = []
            for v in variants:
                try:
                    v_emb = await embedder.embed(v, task_type="query")
                except Exception as e:
                    # A failed variant embed must not sink the recall — skip it.
                    log.warning(f"expansion variant embed failed (skipped): {e}")
                    continue
                extra_passes.append(await _retrieve_for_embedding(v_emb))
            if extra_passes:
                before = top_relevance(all_chunks)
                all_chunks = merge_passes([standard, *extra_passes])
                log.info(
                    "query expansion fired (agent=%s): %d variant(s), "
                    "top relevance %.3f -> %.3f, pool %d",
                    req.agent_id, len(extra_passes), before,
                    top_relevance(all_chunks), len(all_chunks),
                )

        # Re-rank the pooled candidates, then a single trim. Explore mode uses
        # its own self-contained scoring (module constants, no RankingConfig)
        # and therefore works even with composite ranking disabled — a recall
        # mode that silently no-ops would be a silent degradation.
        if mode == "explore":
            # Serendipity lens: adjacency to the pool's top hit, no recency,
            # novelty over familiarity. Zero-scored chunks are the noise band
            # and must not pad the results.
            # v4.21: LEX chunks are in this pool as candidates; the lens
            # deliberately ignores chunk.lexical (so does `recent` below) —
            # adjacency and date are their criteria, not word overlap. A LEX
            # chunk can never raise the pool's top (it was outside the kNN's
            # returned set), so the band constants are undisturbed.
            access = vec_store.access_counts([c.memory_id for c in all_chunks if c.memory_id])
            # E4: the lens reads the same pool-normalised similarity focus
            # mode scores on (cosine, every tier on one scale, anchored on
            # the pool's best hit — ranking.py), so the band constants are
            # span fractions and an L3 hit's raw cosine can no longer set a
            # top the VEC band cannot reach. Wire `relevance` unchanged.
            sims = pool_similarities([(c.relevance, c.cache_tier) for c in all_chunks])
            top_sim = max(sims, default=0.0)
            scored = [
                (explore_score(
                    similarity=sim,
                    top_similarity=top_sim,
                    category=c.category,
                    access_count=access.get(c.memory_id, 0) if c.memory_id else 0,
                ), c)
                for sim, c in zip(sims, all_chunks)
            ]
            all_chunks = [c for s, c in sorted(
                scored, key=lambda sc: sc[0], reverse=True) if s > 0.0]
        elif mode == "recent":
            # v4.18.4 — the boot lens (E1, proving ground S03). "What was I
            # working on" is a RECENCY question asked over near-identical
            # session memories: on the focus lens a 0.06-cosine wording gap
            # (topic nouns) outweighed 13 days of age and the latest session
            # ranked 12th of 13. No weight fixes that without breaking the
            # focus contract (an old exact doctrine beats a fresh marginal
            # state), so it is a different lens: similarity only GATES —
            # anything inside the pool's band (pool_similarities > 0) is
            # on-topic — and the date ORDERS. Newest first, the noise band
            # excluded, unknown age last.
            now = time.time()
            sims = pool_similarities([(c.relevance, c.cache_tier) for c in all_chunks])
            # 4.21.1: a pinned chunk (rare identifier the prompt named) passes
            # the band gate — the prompt picked it, its cosine is beside the point
            in_band = [c for sim, c in zip(sims, all_chunks) if sim > 0.0 or c.exact]
            all_chunks = exact_first(sorted(in_band, key=lambda c: chunk_age_days(c, now)),
                                     limit=max(1, req.max_results // 2))
        elif config.ranking.enabled:
            now = time.time()

            def _age(c: ContextChunk) -> Optional[float]:
                if c.age_days is not None:
                    return c.age_days
                if c.created_at:
                    return (now - float(c.created_at)) / 86400.0
                return None

            access = vec_store.access_counts([c.memory_id for c in all_chunks if c.memory_id])
            # Experiment One: similarity enters the composite normalised over
            # the pool (cosine, anchored on the best hit — ranking.py); raw
            # tier relevance left the term inert. Wire `relevance` unchanged.
            sims = pool_similarities([(c.relevance, c.cache_tier) for c in all_chunks])
            scored = [
                (composite_score(
                    similarity=sim,
                    age_days=_age(c),
                    category=c.category,
                    access_count=access.get(c.memory_id, 0) if c.memory_id else 0,
                    cfg=config.ranking,
                    lexical=c.lexical,
                ), c)
                for sim, c in zip(sims, all_chunks)
            ]
            scored.sort(key=lambda sc: sc[0], reverse=True)
            # 4.21.1: a memory holding a rare identifier the prompt named is
            # served first — the prompt picked it; the score orders the rest.
            # At most half the window: pins re-order it, never own it.
            all_chunks = exact_first([c for _, c in scored], limit=max(1, req.max_results // 2))

        # v4.18.4: inside the served window a memory that revises another
        # served memory (forced past the dedup gate, `near_dup_of`) comes
        # first — the stale truth still serves, after the new one. Membership
        # is the score's decision; this only orders (ranking.order_revisions).
        selected = order_revisions(all_chunks[: req.max_results])
        for c in selected:
            cache_hits[c.cache_tier] = cache_hits.get(c.cache_tier, 0) + 1

        # Served memories earn access credit — feeds the next recall's ranking.
        try:
            vec_store.bump_access([c.memory_id for c in selected if c.memory_id])
        except Exception as e:
            log.warning(f"recall_stats bump failed (non-fatal): {e}")

        latency = (time.time() - start) * 1000
        return ContextResponse(
            chunks=[ContextChunkResponse(**c.to_dict()) for c in selected],
            total_found=len(selected),
            latency_ms=round(latency, 1),
            cache_hits=cache_hits,
            mode=mode,
            agent_id=req.agent_id,
            persona=persona.name,
            provider_used=embedder.active_label,
        )

    # ── Preflight ──
    @app.post("/preflight", response_model=PreflightResponse)
    async def preflight(req: PreflightRequest, request: Request):
        _enforce_scope(request, req.agent_id)
        start = time.time()
        persona = get_persona(config, req.persona, req.agent_id)
        tenant = tenants.get(req.agent_id)
        vec_store: VecStore = tenant["vec"]

        # Same redaction choke point as /writeback + /ingest: the prompt and
        # draft go verbatim to the (possibly remote) reasoner, and this was
        # the one tenant payload that skipped redact_text.
        prompt, p_counts = redact_text(req.prompt)
        draft, d_counts = redact_text(req.draft_response)
        total_red = sum(p_counts.values()) + sum(d_counts.values())
        if total_red:
            log.warning(f"🔒 Redacted {total_red} secret(s) in preflight payload "
                        f"before reasoning ({ {**p_counts, **d_counts} })")

        system_prompt = build_preflight_system_prompt(persona)

        user_prompt = f"USER'S PROMPT:\n{prompt}\n\nAGENT'S DRAFT RESPONSE:\n{draft}\n\nReview and provide your preflight verdict as JSON."

        # Cross-reference memory. Until the E3 follow-up this read the L1
        # precache bundles and the L2 index (top 2 each, cosine >= the
        # persona's L1 threshold, default 0.75 — a bar the embedder's
        # on-topic cosines (~0.6-0.74) essentially never cleared, so the
        # block was empty in practice). Both tiers are retired from recall
        # (E3); the reasoner now sees the two nearest memories from the same
        # VEC index /context serves.
        try:
            query_embedding = await embedder.embed(prompt, task_type="query")
            # Same default hiding as /context: session_log rows (raw
            # auto-capture, archived-session summaries) never feed a verdict.
            hits = vec_store.search(query_embedding, top_k=2,
                                    exclude_categories=DEFAULT_HIDDEN_CATEGORIES)
            if hits:
                context_text = "\n\n".join(f"[VEC] {h.text}" for h in hits)
                user_prompt = f"MEMORY CONTEXT:\n{context_text}\n\n{user_prompt}"
        except Exception as e:
            log.warning(f"Preflight context retrieval failed: {e}")

        try:
            raw = await reasoner.generate(user_prompt, system=system_prompt)
            cleaned = raw.strip().strip("`").strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            result = json.loads(cleaned)
            latency = (time.time() - start) * 1000

            return PreflightResponse(
                # A well-formed reasoner reply always carries a verdict; one
                # without it is malformed, and malformed must not rubber-stamp.
                verdict=result.get("verdict", "UNAVAILABLE").upper(),
                confidence=float(result.get("confidence", 0.5)),
                reason=result.get("reason", ""),
                enrichment=result.get("enrichment"),
                latency_ms=round(latency, 1),
                persona=persona.name,
                provider_used=reasoner.active_label,
            )
        except Exception as e:
            latency = (time.time() - start) * 1000
            log.warning(f"Preflight error: {e}")
            # Fail CLOSED-ish: a reasoner outage or garbage JSON used to
            # return PASS — a validation gate that rubber-stamps exactly when
            # it can't validate. UNAVAILABLE tells the caller no validation
            # happened; the caller decides whether to proceed.
            return PreflightResponse(
                verdict="UNAVAILABLE", confidence=0.0,
                reason=f"AgentB couldn't validate — no verdict available ({str(e)[:80]})",
                latency_ms=round(latency, 1),
                persona=persona.name,
                provider_used=reasoner.active_label,
            )

    # ── Writeback ──
    @app.post("/writeback", response_model=WritebackResponse)
    async def writeback(req: WritebackRequest, request: Request):
        _enforce_scope(request, req.agent_id)
        # Check read-only
        if req.agent_id and req.agent_id in config.agents:
            if config.agents[req.agent_id].read_only:
                raise HTTPException(403, f"Agent '{req.agent_id}' is read-only")

        # v4.1 capture gate: while paused, ambient/auto-capture-shaped writes
        # are DISCARDED — the sensitive window must not be persisted anywhere.
        # Deliberate manual saves (user/inferred, real summary) still land:
        # saving the *why* of a sensitive operation while ambient capture is
        # off is the intended workflow.
        if (req.category == "session_log" or req.source == "tool"
                or is_routine_log(req.summary, req.key_facts)) and gate.is_paused():
            log.warning(f"Writeback discarded (capture paused): {req.session_id}")
            return WritebackResponse(
                status="paused", memory_id="", agent_id=req.agent_id,
                l1_bundles_updated=0,
                message="Capture is paused — ambient writeback discarded (auto-resumes).",
            )

        # v4.1 secret redaction — the single choke point for everything that
        # enters the store. Runs BEFORE classification so a leaked key never
        # rides a classify call out to a remote LLM either.
        red_counts: dict[str, int] = {}

        def _r(text: str) -> str:
            clean, counts = redact_text(text or "")
            for k, v in counts.items():
                red_counts[k] = red_counts.get(k, 0) + v
            return clean

        summary = _r(req.summary)
        key_facts = [_r(f) for f in (req.key_facts or [])]
        decisions_made = [_r(d) for d in (req.decisions_made or [])]
        additional_tags = [_r(t) for t in (req.additional_tags or [])]
        if red_counts:
            log.warning(
                f"🔒 Redacted {sum(red_counts.values())} secret(s) in writeback "
                f"{req.session_id}: " + ", ".join(f"{k}×{v}" for k, v in red_counts.items())
            )

        tenant = tenants.get(req.agent_id)
        memory_dir = tenant["memory_dir"]
        vec_store: VecStore = tenant["vec"]

        ts = req.timestamp or datetime.now(timezone.utc).isoformat()
        # v4.18.2: a caller-supplied timestamp is the memory's birth, not the
        # moment it reached this server. Imports, stick carries and backfills
        # were landing with created_at=now — every recency term (ranker,
        # age_days, stale warnings, dedup age) then saw a 30-day-old memory
        # as newborn.
        # v4.18.3: the fallback is OBSERVABLE (record flag + warning), and a
        # future-dated stamp is clamped to now — an unclamped one pins recency
        # at 1.0 forever, suppresses stale warnings and escapes max_age_days.
        now = time.time()
        created_at, created_at_fallback = now, False
        if req.timestamp:
            parsed = _epoch_from_iso(req.timestamp)
            if parsed is None:
                created_at_fallback = True
                log.warning(f"Writeback {req.session_id}: unparseable timestamp "
                            f"{req.timestamp!r} — created_at falls back to now")
            else:
                created_at = min(parsed, now)

        # Embed once, then use the existing same-tenant index for a bounded
        # top-k candidate check before anything is persisted. SQLite work runs
        # through an isolated worker connection in dedup.py.
        full_text = summary + "\n" + "\n".join(key_facts)
        # v4.18.3: the id mixes in the CONTENT. It used to be session_id:ts
        # alone, so two writes sharing a stamp (every import, carry and
        # backfill that reuses one timestamp) silently overwrote each other —
        # 200 OK, one file on disk. Identical re-sends still map to the same
        # id, so idempotent retries stay idempotent.
        memory_id = hashlib.sha256(
            f"{req.session_id}:{ts}:{full_text}".encode()).hexdigest()[:16]
        embedding = None
        dedup_unchecked = None
        try:
            embedding = await embedder.embed(
                full_text, use_breaker=not req.batch, task_type="document")
        except Exception as exc:
            # Degrade to raw: an embedder outage must never turn dedup into
            # silent write loss, especially for non-interactive capture.
            dedup_unchecked = f"embedding unavailable: {type(exc).__name__}"
            log.error(f"Writeback dedup unavailable for {req.session_id}: {exc}")
        near_duplicates: list[dict] = []
        if config.dedup.enabled and embedding is not None:
            near_duplicates = await find_near_duplicates(
                vec_store=vec_store,
                embedding=embedding,
                text=full_text,
                candidate_id=req.session_id,
                allow_path=memory_dir.parent / "dedup-allow.txt",
                top_k=config.dedup.top_k,
                cosine_threshold=config.dedup.cosine_threshold,
                overlap_threshold=config.dedup.overlap_threshold,
                min_tokens=config.dedup.min_tokens,
            )
        non_interactive = bool(
            req.batch or req.category == "session_log"
            or is_routine_log(summary, key_facts))
        if near_duplicates and not non_interactive and not req.force and not req.supersedes:
            return WritebackResponse(
                status="held", memory_id="", agent_id=req.agent_id,
                l1_bundles_updated=0,
                message=("Near-duplicate held. Repeat with force=true, "
                         "supersedes=[id], or abandon the save."),
                near_duplicates=near_duplicates,
                redactions=sum(red_counts.values()),
            )

        # v3: provenance + decay tagging
        source_used = req.source if req.source in VALID_SOURCES else "inferred"
        # v4 Smart Ingestion: categorize so real memories (Tier 1) never land in
        # the same bucket as raw session logs (Tier 2). Resolution order:
        #   1. explicit caller category always wins
        #   2. LLM classification (cheap noise pre-filter demotes logs for free)
        #   3. legacy regex suggester (when classification disabled or LLM down)
        classified_by = None
        needs_reclassification = False
        if req.category and req.category in VALID_CATEGORIES:
            category_used = req.category
            category_suggested_field = None
            category_match_keywords_field = None
        elif config.classification.enabled:
            category_used, classified_by = await classify_category(
                reasoner, summary, key_facts,
                use_breaker=not req.batch,
                max_input_chars=config.classification.max_input_chars,
            )
            needs_reclassification = classified_by == "regex"
            category_suggested_field = category_used
            category_match_keywords_field = None
        else:
            suggestion_text = summary + "\n" + "\n".join(key_facts)
            category_used, suggestion_keywords = suggest_category(suggestion_text)
            classified_by = "regex"
            category_suggested_field = category_used
            category_match_keywords_field = suggestion_keywords

        memory_entry = {
            "id": memory_id, "session_id": req.session_id,
            "agent_id": req.agent_id, "summary": summary,
            "key_facts": key_facts,
            "projects_referenced": req.projects_referenced,
            "decisions_made": decisions_made,
            "timestamp": ts, "created_at": created_at,
            "ingested_at": now,          # v4.18.3: when it reached THIS server (rulekeeper window)
            # v3 fields
            "source": source_used,
            "category": category_used,
            "additional_tags": additional_tags,
            "schema_version": 3,
        }
        if created_at_fallback:
            memory_entry["created_at_fallback"] = True
        near_dup_ids = [match["id"] for match in near_duplicates]
        if near_dup_ids:
            memory_entry["near_dup_of"] = near_dup_ids
            # v4.18.4: only a write the CALLER forced past the hold is a
            # revision of what it was held against; a batch import or a
            # routine-log-shaped write skipped the hold and chose nothing.
            if req.force:
                memory_entry["near_dup_forced"] = True
        if req.supersedes:
            memory_entry["supersedes"] = list(dict.fromkeys(req.supersedes))
        if dedup_unchecked:
            memory_entry["dedup_unchecked"] = dedup_unchecked
        # v4 provenance: how the category was decided, and whether the dreamer
        # should revisit it (regex fallback fired because the LLM was unavailable).
        if classified_by:
            memory_entry["classified_by"] = classified_by
        if needs_reclassification:
            memory_entry["needs_reclassification"] = True
        # Off-loop + atomic: the dump/write used to run on the event loop
        # (blocking every concurrent request), and a plain write_text leaves
        # a torn-read window for on-loop readers (precache, analyst) now that
        # the write can interleave with them.
        await asyncio.to_thread(
            _atomic_write_text, memory_dir / f"{memory_id}.json",
            json.dumps(memory_entry, indent=2, default=str))
        # v4.20: seal the testimony in the tenant's ledger (append + fsync,
        # off-loop). The record is on disk first: a crash between the two
        # leaves an unsealed record, which verify() names — never a sealed
        # entry with no record behind it.
        await asyncio.to_thread(get_ledger(memory_dir).seal, memory_entry)

        # Demotion keeps the old JSON as audit history while removing it from
        # active vector recall. No memory evidence is deleted.
        for old_id in dict.fromkeys(req.supersedes):
            old_path = memory_dir / f"{old_id}.json"
            if not old_path.exists():
                continue

            def _mark_superseded(path=old_path, new_id=memory_id):
                old = json.loads(path.read_text(encoding="utf-8"))
                old["superseded_by"] = new_id
                old["superseded_at"] = time.time()
                _atomic_write_text(path, json.dumps(old, indent=2, default=str))

            await asyncio.to_thread(_mark_superseded)
            vec_store.delete(old_id)
        log.info(f"Writeback: {req.session_id} → {memory_id} (agent: {req.agent_id or 'default'}, source={source_used}, category={category_used})")

        try:
            if embedding is None:
                raise RuntimeError("embedding unavailable; raw memory retained unindexed")
            # v4.1: new memories index into VEC only. The legacy L2 tier kept a
            # full copy of every embedding in one index.json rewritten on every
            # save (cc's had grown to 43 MB → ~43 MB of disk writes per minute
            # under the 60s auto-sync) and resurrected deleted memories. L2
            # remains read-only for pre-v4.1 entries until a backfill retires it.

            try:
                vec_store.upsert(
                    memory_id,
                    full_text,
                    embedding,
                    source_file=(memory_dir / f"{memory_id}.json").as_posix(),
                    created_at=created_at,
                    category=category_used,  # #468: mirror category into the search pre-filter column
                )
            except VecDimMismatch as e:
                # Dim mismatch is a configuration/contract bug, not a runtime
                # blip. Surface to the caller — silent vector loss is the
                # exact failure mode the dim guard was added to prevent.
                log.error(f"vec_index dim mismatch on writeback {memory_id}: {e}")
                raise HTTPException(500, f"vec index dim mismatch: {e}")

            # E3 follow-up: the per-project L1 bundles this used to write
            # (one embed per referenced project) fed a tier /context no
            # longer reads — dead writes. `l1_bundles_updated` stays on the
            # wire and reports 0.
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Writeback indexing failed: {e}")

        return WritebackResponse(
            status="archived", memory_id=memory_id, agent_id=req.agent_id,
            l1_bundles_updated=0,  # wire compatibility: the L1 tier is gone
            message=f"Session {req.session_id} archived for agent '{req.agent_id or 'default'}'.",
            category_used=category_used,
            category_suggested=category_suggested_field,
            category_match_keywords=category_match_keywords_field,
            source_used=source_used,
            redactions=sum(red_counts.values()),
            near_duplicates=near_duplicates,
        )

    # ── Pocket Mnemo: one chat turn (v4.19) ──
    # recall → reason → save, all inside the caller's tenant, reusing the
    # /context and /writeback handlers so every rule they enforce (scope pin,
    # read-only, capture gate, redaction, dedup hold) applies to the phone too.
    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request):
        # A scoped token pinned to one tenant may omit agent_id: the pin IS the
        # identity (the phone only ever holds a token). Master-token callers
        # still name their tenant, as everywhere else.
        agent_id = req.agent_id or getattr(request.state, "scoped_agent_id", None)
        _enforce_scope(request, agent_id)
        if agent_id is None and tenants.has_named_tenants():
            raise HTTPException(
                400, "agent_id is required on a multi-tenant installation")
        if req.save and agent_id and agent_id in config.agents \
                and config.agents[agent_id].read_only:
            raise HTTPException(403, f"Agent '{agent_id}' is read-only")
        try:
            if agent_id is not None:
                validate_agent_id(agent_id)
            session_id = (validate_session_id(req.session_id) if req.session_id
                          else f"pocket-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8]}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        start = time.time()
        persona = get_persona(config, req.persona, agent_id)

        # Same redaction choke point as /writeback, /ingest and /preflight: the
        # message and history go verbatim to a (possibly remote) reasoner, so
        # a pasted key must be scrubbed BEFORE it leaves the machine — the
        # save's own redaction would be too late (review, 2026-09-05).
        red_counts: dict[str, int] = {}

        def _r(text: str) -> str:
            clean, counts = redact_text(text or "")
            for k, v in counts.items():
                red_counts[k] = red_counts.get(k, 0) + v
            return clean

        message = _r(req.message)
        history = [ChatTurn(role=t.role, content=_r(t.content)) for t in req.history]
        if red_counts:
            log.warning(f"🔒 Redacted {sum(red_counts.values())} secret(s) in /chat "
                        f"{session_id} before reasoning: "
                        + ", ".join(f"{k}×{v}" for k, v in red_counts.items()))

        memories: list[ChatMemory] = []
        if req.max_memories:
            # Hide nothing: every memory in a pocket tenant came through this
            # door, and the classifier may file a chat-shaped turn as
            # session_log — which /context hides by default. A door that
            # forgets what it was told is the one failure this product cannot
            # have (review, 2026-09-05).
            ctx = await context(ContextRequest(
                prompt=message, agent_id=agent_id, persona=req.persona,
                max_results=req.max_memories, exclude_categories=[]), request)
            memories = [
                ChatMemory(memory_id=c.memory_id, content=c.content[:POCKET_MEMORY_CHARS],
                           category=c.category, age_days=c.age_days, relevance=c.relevance)
                for c in ctx.chunks]

        prompt = _pocket_prompt(memories, history, message)
        try:
            reply = (await reasoner.generate(
                prompt, system=POCKET_SYSTEM, max_tokens=POCKET_MAX_TOKENS)).strip()
        except Exception as e:
            log.error(f"/chat reasoning failed: {e}")
            raise HTTPException(503, "reasoning unavailable: every provider failed")
        if not reply:
            raise HTTPException(503, "reasoning returned an empty reply")

        # The person's own words ARE the memory (source=user); the reply rides
        # along as a labelled key_fact so the turn reads whole on recall but
        # the model's words are never attributed to the person. Every refusal
        # is reported in save_status — never a silent drop.
        save_status, memory_id = "skipped", ""
        if req.save and gate.is_paused():
            # The capture-pause switch means "nothing lands during this
            # window" — the phone honours it like every other writer.
            log.warning(f"/chat save discarded (capture paused): {session_id}")
            save_status = "paused"
        elif req.save:
            reply_cut = reply
            if len(reply_cut) > POCKET_REPLY_IN_MEMORY:
                reply_cut = reply_cut[:POCKET_REPLY_IN_MEMORY].rsplit(" ", 1)[0] + "…"
            wb = WritebackRequest(
                session_id=session_id, agent_id=agent_id,
                summary=message, key_facts=[f"Pocket replied: {reply_cut}"],
                source="user", additional_tags=["pocket"])
            try:
                saved = await writeback(wb, request)
                save_status, memory_id = saved.status, saved.memory_id
            except HTTPException as e:
                log.error(f"/chat save refused ({e.status_code}): {e.detail}")
                save_status = "failed"
            except Exception as e:
                log.error(f"/chat save failed: {e}")
                save_status = "failed"

        return ChatResponse(
            reply=reply, session_id=session_id, agent_id=agent_id,
            persona=persona.name, memories=memories,
            save_status=save_status, memory_id=memory_id,
            latency_ms=round((time.time() - start) * 1000, 1),
            provider_used=reasoner.active_label,
        )

    # ── Pocket Mnemo app (the phone door) ──
    # Served by the memory server itself: one origin, no CORS, nothing new
    # listens. The shell holds no secrets (see check_auth), and only the
    # allowlisted files exist — no traversal, no listing.
    @app.get("/app", include_in_schema=False)
    async def pocket_app_redirect():
        return RedirectResponse("/app/", status_code=307)

    @app.get("/app/", include_in_schema=False)
    async def pocket_app_index():
        return FileResponse(POCKET_APP_DIR / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/app/{asset}", include_in_schema=False)
    async def pocket_app_asset(asset: str):
        media = POCKET_APP_ASSETS.get(asset)
        if media is None:
            raise HTTPException(404, "not found")
        return FileResponse(POCKET_APP_DIR / asset, media_type=media,
                            headers={"Cache-Control": "no-cache"})

    # ── Trajectory Learning (v4.5) ──
    @app.post("/trajectory/save", response_model=TrajectorySaveResponse)
    async def trajectory_save(req: TrajectorySaveRequest, request: Request):
        """Capture a proven task recipe AFTER the agent judges it succeeded."""
        _enforce_scope(request, req.agent_id)
        if req.agent_id and req.agent_id in config.agents:
            if config.agents[req.agent_id].read_only:
                raise HTTPException(403, f"Agent '{req.agent_id}' is read-only")

        tenant = tenants.get(req.agent_id)
        traj: TrajectoryStore = tenant["trajectories"]
        steps = [s.model_dump() for s in req.steps]

        # Embed the recipe so it's recallable by NL description. Any embedder
        # failure must surface — a saved-but-unindexed trajectory would never be
        # recalled (silent loss is the failure mode Vapor Truth forbids).
        text = traj_embedding_text(req.task_description, req.outcome, steps)
        try:
            embedding = await embedder.embed(text, task_type="document")
        except Exception as e:
            log.error(f"Trajectory embed failed (agent={req.agent_id}): {e}")
            raise HTTPException(503, f"Embedder unavailable, trajectory not saved: {e}")

        try:
            record = traj.save(
                agent_id=req.agent_id,
                task_type=req.task_type,
                task_description=req.task_description,
                steps=steps,
                outcome=req.outcome,
                rating=req.rating,
                embedding=embedding,
                token_cost=req.token_cost,
                model=req.model,
                duration_seconds=req.duration_seconds,
                derived_from=req.derived_from,
                source=req.source,
                evidence_source=req.evidence_source,
            )
        except VecDimMismatch as e:
            log.error(f"Trajectory vec dim mismatch (agent={req.agent_id}): {e}")
            raise HTTPException(500, f"vec index dim mismatch: {e}")

        return TrajectorySaveResponse(
            status="saved",
            trajectory_id=record["id"],
            agent_id=req.agent_id,
            task_type=req.task_type,
            total_for_agent=traj.count(),
            message=(
                f"Trajectory '{req.task_type}' saved for agent "
                f"'{req.agent_id or 'default'}' (rating {req.rating})."
            ),
        )

    @app.post("/trajectory/recall", response_model=TrajectoryRecallResponse)
    async def trajectory_recall(req: TrajectoryRecallRequest, request: Request):
        """Recall proven task recipes BEFORE a similar task."""
        _enforce_scope(request, req.agent_id)
        tenant = tenants.get(req.agent_id)
        traj: TrajectoryStore = tenant["trajectories"]

        try:
            query_embedding = await embedder.embed(req.query, task_type="query")
        except Exception as e:
            log.error(f"Trajectory recall embed failed (agent={req.agent_id}): {e}")
            raise HTTPException(503, f"Embedder unavailable: {e}")

        results = traj.recall(
            query_embedding,
            task_type=req.task_type,
            min_rating=req.min_rating,
            max_results=req.max_results,
        )
        return TrajectoryRecallResponse(
            trajectories=results,
            total_found=len(results),
            agent_id=req.agent_id,
        )

    # ── Ingest (The Live Wire) ──
    @app.post("/ingest", response_model=IngestResponse)
    async def ingest(req: IngestRequest):
        """
        The Live Wire — capture every prompt/response as it happens.
        Call this after every exchange. Fast (<5ms), append-only, crash-safe.
        If the plug gets pulled, everything up to the last ingest is on disk.
        """
        # Check read-only
        if req.agent_id and req.agent_id in config.agents:
            if config.agents[req.agent_id].read_only:
                raise HTTPException(403, f"Agent '{req.agent_id}' is read-only")

        # v4.1 capture gate: /ingest is pure ambient capture — while paused,
        # every exchange in the window is discarded by design.
        if gate.is_paused():
            return IngestResponse(
                status="paused", session_id="", entry_number=0,
                agent_id=req.agent_id,
            )

        # v4.1 secret redaction at the live-wire choke point.
        prompt, p_counts = redact_text(req.prompt)
        response, r_counts = redact_text(req.response)
        metadata, m_counts = redact_obj(req.metadata) if req.metadata else (None, {})
        total_redactions = sum(p_counts.values()) + sum(r_counts.values()) + sum(m_counts.values())
        if total_redactions:
            log.warning(f"🔒 Redacted {total_redactions} secret(s) in /ingest "
                        f"(agent: {req.agent_id or 'default'})")

        tenant = tenants.get(req.agent_id)
        sessions = tenant["sessions"]

        result = sessions.ingest(
            prompt=prompt,
            response=response,
            metadata=metadata,
        )

        # "duplicate" = retry of an already-captured exchange; still a 200 so
        # clients treat it as delivered and advance their offsets.
        return IngestResponse(
            status=result.get("status", "captured"),
            session_id=result["session_id"],
            entry_number=result["entry_number"],
            agent_id=req.agent_id,
            redactions=total_redactions,
        )

    # ── Capture pause gate (v4.1) ──
    class CapturePauseRequest(BaseModel):
        minutes: Optional[int] = Field(
            None, ge=1, description="Pause duration; default 15, hard cap 240."
        )
        reason: str = Field("", description="Why capture is paused (shown in status).")

    @app.post("/capture/pause")
    async def capture_pause(req: CapturePauseRequest):
        return gate.pause(req.minutes, req.reason)

    @app.post("/capture/resume")
    async def capture_resume():
        return gate.resume()

    @app.get("/capture/status")
    async def capture_status():
        return gate.status()

    # ── Dream brief (v4.9.3) ──
    @app.get("/dream/latest")
    async def dream_latest():
        """Serve the newest dream brief markdown.

        The dreamer writes <data_dir>/dreams/YYYY-MM-DD.md on THIS host.
        Bridges used to read that directory from their own local disk, which
        broke silently the day the dreamer moved to a different machine than
        the agents. Serving it over HTTP makes the Cortex the single source
        of dreams; the bridge keeps its local read as an offline fallback.
        """
        dream_dir = Path(config.data_dir) / "dreams"

        def _read_latest():
            # Directory walk + file read off the event loop — a slow or large
            # dreams dir must not stall unrelated requests.
            try:
                candidates = sorted(
                    (p for p in dream_dir.glob("*.md") if not p.stem.endswith("-boot")),
                    key=lambda p: p.name, reverse=True
                )
            except OSError:
                candidates = []
            if not candidates:
                return None
            p = candidates[0]
            boot_path = p.with_name(f"{p.stem}-boot.md")
            boot_content = boot_path.read_text(encoding="utf-8") if boot_path.exists() else None
            return p.stem, p.read_text(encoding="utf-8"), boot_content, p.stat().st_mtime

        try:
            found = await asyncio.to_thread(_read_latest)
        except OSError as e:
            raise HTTPException(500, f"Dream brief unreadable: {e}")
        if found is None:
            raise HTTPException(404, "No dream briefs on disk")
        stem, content, boot_content, mtime = found
        return {
            "date": stem,
            "age_hours": round((time.time() - mtime) / 3600, 1),
            "content": content,
            "boot_content": boot_content,
        }

    # ── Session Info ──
    @app.get("/sessions")
    async def list_sessions(agent_id: Optional[str] = None):
        """List all sessions across tiers for an agent."""
        tenant = tenants.get(agent_id)
        sessions = tenant["sessions"]
        return {
            "agent_id": agent_id or "default",
            "hot": sessions.get_hot_sessions(),
            "warm": sessions.get_warm_sessions(),
            "stats": sessions.stats,
        }

    @app.get("/sessions/{session_id}/transcript")
    async def get_transcript(session_id: str, agent_id: Optional[str] = None):
        """Get full transcript of a specific session."""
        tenant = tenants.get(agent_id)
        sessions = tenant["sessions"]
        try:
            entries = sessions.get_session_transcript(session_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not entries:
            raise HTTPException(404, "Session not found")
        exchanges = [e for e in entries if e.get("_type") == "exchange"]
        return {
            "session_id": session_id,
            "agent_id": agent_id or "default",
            "exchanges": len(exchanges),
            "transcript": entries,
        }

    @app.get("/sessions/recent")
    async def recent_context(agent_id: Optional[str] = None, n: int = 20):
        """Get most recent exchanges as plain text (for bootstrap injection)."""
        tenant = tenants.get(agent_id)
        sessions = tenant["sessions"]
        return {
            "agent_id": agent_id or "default",
            "context": sessions.get_recent_context(n),
        }

    # ── Vec index management ──
    @app.get("/vec/status")
    async def vec_status(agent_id: Optional[str] = None):
        tenant = tenants.get(agent_id)
        vec_store: VecStore = tenant["vec"]
        memory_dir = tenant["memory_dir"]
        on_disk = sum(1 for _ in memory_dir.glob("*.json"))
        return {
            "agent_id": agent_id or "default",
            "mode": tenant["vec_mode"],
            "indexed": vec_store.count(),
            "memory_entries_on_disk": on_disk,
            "db_path": vec_store.db_path.as_posix(),
        }

    @app.post("/vec/backfill")
    async def vec_backfill_endpoint(agent_id: Optional[str] = None, skip_existing: bool = True):
        tenant = tenants.get(agent_id)
        vec_store: VecStore = tenant["vec"]
        memory_dir = tenant["memory_dir"]
        # Bypass the resilient wrapper's circuit breaker — backfill is a long
        # batch over heterogeneous content that historically trips the breaker
        # after a few oversize entries, which would then poison live /context
        # queries with "circuit open" until the cooldown elapses. Calling the
        # primary embedder directly isolates per-entry failures.
        stats = await vec_backfill(
            vec_store,
            memory_dir,
            lambda t: embedder.primary.embed(t, task_type="document"),
            skip_existing=skip_existing,
        )
        return {"agent_id": agent_id or "default", **stats}

    # ── Ledger (v4.20): tamper-evident chain over the testimony ──
    class LedgerSealRequest(BaseModel):
        agent_id: Optional[str] = Field(
            None, description="Memory tenant. A scoped token may omit it — the pin decides.")

    def _ledger_tenant(request: Request, agent_id: Optional[str]) -> tuple[Optional[str], dict]:
        """(resolved agent_id, tenant). A scoped token may omit agent_id —
        the pin fills it — and the report names the resolved tenant."""
        pinned = getattr(request.state, "scoped_agent_id", None)
        if agent_id is None and pinned is not None:
            agent_id = pinned
        _enforce_scope(request, agent_id)
        if agent_id is None and tenants.has_named_tenants():
            raise HTTPException(
                400, "agent_id is required on a multi-tenant installation")
        return agent_id, tenants.get(agent_id)

    @app.get("/ledger/verify")
    async def ledger_verify(request: Request, agent_id: Optional[str] = None):
        """Walk the tenant's ledger chain and check every sealed record
        against disk. Read-only; safe at any time."""
        agent_id, tenant = _ledger_tenant(request, agent_id)
        memory_dir = tenant["memory_dir"]
        report = await asyncio.to_thread(get_ledger(memory_dir).verify, memory_dir)
        return {"agent_id": agent_id, "ledger": get_ledger(memory_dir).path.as_posix(),
                **report.to_dict()}

    @app.post("/ledger/seal")
    async def ledger_seal(request: Request, req: LedgerSealRequest):
        """Adopt every unsealed record in the tenant (pre-ledger stores,
        stick carries). Altered records are never re-sealed here, and a
        broken chain is refused with 409 + the report."""
        agent_id, tenant = _ledger_tenant(request, req.agent_id)
        memory_dir = tenant["memory_dir"]
        ledger = get_ledger(memory_dir)
        try:
            sealed = await asyncio.to_thread(ledger.seal_unsealed, memory_dir)
        except LedgerBroken as exc:
            # A broken chain cannot adopt: the "unsealed" records may be
            # forgeries whose seals were deleted. 409 carries the report.
            raise HTTPException(409, {"error": str(exc), "agent_id": agent_id,
                                      **exc.report.to_dict()})
        report = await asyncio.to_thread(ledger.verify, memory_dir)
        return {"agent_id": agent_id, "sealed_now": len(sealed),
                "sealed_ids": sealed, **report.to_dict()}

    # ── Phase 3: Facts ──
    class FactSaveRequest(BaseModel):
        entity: str
        attribute: str
        value: str
        confidence: str
        evidence_source: str
        source_memory_id: Optional[str] = None
        source_agent: Optional[str] = None

    class FactDemoteRequest(BaseModel):
        entity: str
        attribute: str
        reason: str
        changed_by: Optional[str] = None

    @app.get("/facts/{entity}/{attribute}")
    async def facts_get(entity: str, attribute: str, include_false: bool = False):
        fact = facts.get(entity, attribute, include_false=include_false)
        if fact is None:
            return {"found": False}
        return {"found": True, **fact.to_dict()}

    @app.get("/facts")
    async def facts_query(
        entity: Optional[str] = None,
        attribute: Optional[str] = None,
        value_contains: Optional[str] = None,
        confidence: Optional[str] = None,
        changed_since: Optional[float] = None,
        limit: int = 20,
    ):
        if confidence is not None and confidence not in CONFIDENCE_LEVELS:
            raise HTTPException(400, f"confidence must be one of {CONFIDENCE_LEVELS}")
        results = facts.query(
            entity=entity, attribute=attribute, value_contains=value_contains,
            confidence=confidence, changed_since=changed_since, limit=limit,
        )
        return {"facts": [f.to_dict() for f in results], "count": len(results)}

    @app.post("/facts")
    async def facts_save(req: FactSaveRequest):
        try:
            result = facts.save(
                entity=req.entity, attribute=req.attribute, value=req.value,
                confidence=req.confidence, evidence_source=req.evidence_source,
                source_memory_id=req.source_memory_id, source_agent=req.source_agent,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "written": result.written,
            "was_contradiction": result.was_contradiction,
            "previous_value": result.previous_value,
            "previous_confidence": result.previous_confidence,
            "reason": result.reason,
        }

    @app.post("/facts/demote")
    async def facts_demote(req: FactDemoteRequest):
        try:
            result = facts.demote(req.entity, req.attribute, req.reason, changed_by=req.changed_by)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "written": result.written,
            "previous_value": result.previous_value,
            "previous_confidence": result.previous_confidence,
            "reason": result.reason,
        }

    @app.get("/facts/history/{entity}/{attribute}")
    async def facts_history(entity: str, attribute: str, limit: int = 50):
        return {"history": facts.history(entity, attribute, limit=limit)}

    @app.get("/facts/contradictions")
    async def facts_contradictions(since: Optional[float] = None, limit: int = 100):
        return {"contradictions": facts.contradictions(since=since, limit=limit)}

    # ── Background: precache + session archival ──
    async def maintenance_cycle(cycle: int):
        # v4 dreamer: reclassify stragglers ~hourly (every 12th 5-min cycle),
        # not every cycle — this is a safety net behind Track 1 + the migration.
        do_reclassify = config.classification.enabled and cycle % 12 == 0
        # v4.1 Analyst: distill Tier-2 session logs into Tier-1 notes.
        # Offset half a period from the reclassify pass so the two LLM
        # batch jobs never land on the same cycle.
        do_analyze = (
            config.analysis.enabled
            and cycle % config.analysis.interval_cycles
            == config.analysis.interval_cycles // 2
        )
        # v4.8 Muse: the Analyst's creative sibling lens. Offset by one
        # extra cycle so it never lands on a reclassify (multiples of 12)
        # or Analyst (6 + 12k) cycle — three LLM batch jobs, three lanes.
        do_muse = (
            config.muse.enabled
            and cycle % config.muse.interval_cycles
            == config.muse.interval_cycles // 2 + 1
        )

        # Snapshot: a tenant created by a live request mid-cycle (this loop
        # awaits constantly) would otherwise raise "dict changed size during
        # iteration" — and that RuntimeError used to kill the whole loop.
        # A brand-new tenant is picked up on the next cycle.
        for tenant_key, tenant in list(tenants._tenants.items()):
            sessions = tenant["sessions"]
            memory_dir = tenant["memory_dir"]

            # E3 follow-up: the L1 precache step that ran here (embed the 10
            # most recent memories into bundles every cycle) is gone — the
            # tier it filled is no longer read by /context or /preflight.

            # Archive expired hot sessions → warm, summarizing each with
            # the reasoner so warm sessions carry a real summary instead of
            # the empty string the unwired hook used to leave behind.
            # use_breaker=False: background batch work.
            async def _summarize_session(transcript: str) -> str:
                return await reasoner.generate(
                    transcript[-8000:],
                    system=("Summarize this agent session in 2-4 dense sentences: "
                            "what was worked on, decided, and shipped. No fluff."),
                    max_tokens=300, use_breaker=False,
                )
            try:
                archived = await sessions.archive_hot_sessions(_summarize_session)
                if archived:
                    log.info(f"Archived {len(archived)} hot sessions for '{tenant_key}'")
                # Each archived summary becomes a Tier-2 session_log memory
                # in VEC — this is a hot session's FIRST appearance in
                # /context recall: since E3 retired the HOT keyword tier, the
                # raw exchanges of a live session (≤ hot_days old) are reachable
                # only through /sessions/{id} and /sessions/recent, never by
                # search, until this summary lands. Without it the session
                # would vanish from recall entirely (its old home was the
                # retired L2 write path). The Analyst distills these like
                # any other session log on its next pass.
                for arch in archived:
                    if not arch.get("summary"):
                        continue
                    try:
                        # v4.1 (review fix): the reasoner-generated warm summary is the
                        # one LLM-derived write that didn't pass the redaction
                        # choke point /writeback + /ingest use. Run it through
                        # the same module so a summary can't reintroduce a
                        # secret (defense-in-depth: the source already came
                        # through redacted ingest, but the LLM output is new).
                        summary, _sum_red = redact_text(arch["summary"])
                        if sum(_sum_red.values()):
                            log.warning(
                                f"🔒 Redacted {sum(_sum_red.values())} secret(s) in "
                                f"warm summary for '{tenant_key}': {dict(_sum_red)}")
                        mid = hashlib.sha256(
                            f"archived:{arch['session_id']}".encode()
                        ).hexdigest()[:16]
                        entry = {
                            "id": mid,
                            "session_id": arch["session_id"],
                            "agent_id": tenant_key,
                            "summary": summary,
                            "key_facts": arch.get("key_facts", []),
                            "projects_referenced": [],
                            "decisions_made": [],
                            "timestamp": arch.get("archived_at", ""),
                            "created_at": time.time(),
                            "source": "tool",
                            "category": "session_log",
                            "additional_tags": ["archived-session"],
                            "schema_version": 3,
                        }
                        (memory_dir / f"{mid}.json").write_text(
                            json.dumps(entry, indent=2, default=str),
                            encoding="utf-8")
                        await asyncio.to_thread(get_ledger(memory_dir).seal, entry)
                        emb = await embedder.embed(summary, use_breaker=False, task_type="document")
                        tenant["vec"].upsert(
                            mid, summary, emb,
                            source_file=(memory_dir / f"{mid}.json").as_posix(),
                            created_at=time.time(),
                            category="session_log",  # #468: matches the entry's category
                        )
                    except Exception as e:
                        log.warning(f"Archived-session indexing failed for '{tenant_key}': {e}")
            except Exception as e:
                log.warning(f"Session archival error for '{tenant_key}': {e}")

            # Move expired warm → cold
            try:
                moved = sessions.archive_warm_to_cold()
                if moved:
                    log.info(f"Cold-archived {len(moved)} sessions for '{tenant_key}'")
            except Exception as e:
                log.warning(f"Cold archival error for '{tenant_key}': {e}")

            # v4 dreamer: reclassify uncategorized / regex-fallback stragglers.
            # use_breaker=False so this batch can't trip the live reasoning
            # breaker (batch-vs-live isolation); capped per cycle.
            if do_reclassify:
                try:
                    _vec = tenant["vec"]
                    rstats = await reclassify_memory_dir(
                        tenant["memory_dir"], reasoner,
                        limit=config.classification.dreamer_max_per_cycle,
                        max_input_chars=config.classification.max_input_chars,
                        use_breaker=False,
                        # #468: keep vec_sources.category in step with the JSON
                        on_reclassified=_vec.update_category,
                    )
                    if rstats["reclassified"]:
                        log.info(
                            f"Dreamer reclassified {rstats['reclassified']} memories "
                            f"for '{tenant_key}' ({rstats['by_category']})"
                        )
                except Exception as e:
                    log.warning(f"Reclassification pass error for '{tenant_key}': {e}")

            # v4.1 Analyst pass — the smart-session-analysis layer.
            if do_analyze:
                try:
                    astats = await analyze_tenant(
                        tenant_key, tenant["memory_dir"], tenant["vec"],
                        reasoner, embedder, config=config.analysis,
                    )
                    if astats["scanned"]:
                        log.info(
                            f"Analyst '{tenant_key}': read {astats['scanned']} logs → "
                            f"{astats['notes_saved']} notes saved "
                            f"({astats['notes_deduped']} already known, "
                            f"{astats['failed']} failed)"
                        )
                except Exception as e:
                    log.warning(f"Analyst pass error for '{tenant_key}': {e}")

            # v4.8 Muse pass — creative idea-seed extraction (gate:
            # config.muse.enabled, default OFF pending Guy's-Gate).
            if do_muse:
                try:
                    mstats = await muse_tenant(
                        tenant_key, tenant["memory_dir"], tenant["vec"],
                        reasoner, embedder, config=config.muse,
                    )
                    if mstats["scanned"]:
                        log.info(
                            f"Muse '{tenant_key}': read {mstats['scanned']} logs → "
                            f"{mstats['notes_saved']} idea(s) saved "
                            f"({mstats['notes_deduped']} already known, "
                            f"{mstats['failed']} failed)"
                        )
                except Exception as e:
                    log.warning(f"Muse pass error for '{tenant_key}': {e}")

    async def maintenance_loop():
        cycle = 0
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            cycle += 1
            try:
                await maintenance_cycle(cycle)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad cycle (a torn memory JSON, a provider blowup mid-
                # batch) must not end background maintenance forever.
                log.error(f"Maintenance cycle {cycle} failed: {e!r}")

    # Exposed for tests: run a single cycle deterministically against the
    # app's real tenant manager without waiting on the 5-minute timer.
    app.state.maintenance_cycle = maintenance_cycle
    app.state.tenants = tenants

    # ── Passport Lane (Phase 1) ──
    # Five MCP-facing routes under /passport/*. Self-contained: no shared state
    # with Mnemo's L1/L2/L3 cache. Data lives at $MNEMO_PASSPORT_DIR
    # (default ~/.mnemo/passport), auto-committed via git, never auto-pushed.
    from passport.api import router as passport_router
    app.include_router(passport_router)
    log.info("  Passport Lane: /passport/* endpoints active (5 tools)")

    return app


if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    assert_safe_auth_posture(cfg)
    port = int(os.environ["MNEMO_PORT"]) if os.environ.get("MNEMO_PORT") else cfg.server.port
    uvicorn.run("agentb.server:create_app", factory=True, host=cfg.server.host, port=port,
                reload=False, log_level=cfg.log_level)
