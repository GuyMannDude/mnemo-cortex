"""
Mnemo Cortex v3.0.0 — Drop-in Memory Superhero for AI Agents
=============================================================
Every AI agent has amnesia. Mnemo Cortex is the cure.
Five endpoints. Any LLM. Total recall.

  /health      → System status + provider failover state + session stats
  /context     → Persona-aware L1/L2/L3 + hot session search
  /preflight   → Persona-aware PASS / ENRICH / WARN / BLOCK
  /ingest      → Live wire: capture every prompt/response as it happens
  /writeback   → Curated session archiving (still works, complementary)

https://github.com/GuyMannDude/mnemo-cortex
"""

import os
import re
import json
import time
import hashlib
import hmac
import logging
import asyncio
import statistics
import httpx
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agentb.config import (
    load_config, AgentBConfig, get_agent_data_dir, get_persona, PersonaConfig,
    ExpansionConfig,
)
from agentb.providers import create_resilient_reasoning, create_resilient_embedding
from agentb.cache import L1Cache, L2Index, l3_scan, ContextChunk, resolve_disk_truth
from agentb.sessions import SessionManager, SessionConfig
from agentb.provenance import (
    VALID_SOURCES, VALID_CATEGORIES, DEFAULT_HIDDEN_CATEGORIES,
    suggest_category, compute_stale_warning,
)
from agentb.classify import classify_category, reclassify_memory_dir, is_routine_log
from agentb.redact import redact_text, redact_obj
from agentb.capture_gate import CaptureGate
from agentb.ranking import composite_score, explore_score
from agentb.analyst import analyze_tenant, muse_tenant
from agentb.vec import VecStore, detect_mode as vec_detect_mode, backfill as vec_backfill, VecDimMismatch
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
    prompt: str = Field(..., description="The prompt to search context for")
    agent_id: Optional[str] = Field(None, description="Agent ID for tenant isolation")
    persona: Optional[str] = Field(None, description="Persona mode: default, strict, creative")
    max_results: int = Field(5, ge=1, le=20)
    mode: str = Field(
        "focus",
        pattern="^(focus|explore)$",
        description=(
            "Recall lens. 'focus' (default): best match wins — similarity + "
            "recency + importance + access. 'explore' (the serendipity lens): "
            "what does this remind the store of — prefers the adjacent "
            "similarity band, ignores recency, favors rarely-recalled "
            "memories. Use explore for brainstorming and idea recall."
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
            "Thesaurus Loop query expansion. None = server default; True = allow; "
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
    agent_id: Optional[str]
    persona: str
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
        key = agent_id or "default"
        if key in self._tenants:
            return self._tenants[key]

        data_dir = get_agent_data_dir(self.config, agent_id)
        memory_dir = data_dir / "memory"
        l1_dir = data_dir / "cache" / "l1"
        l2_dir = data_dir / "cache" / "l2"

        for d in [memory_dir, l1_dir, l2_dir]:
            d.mkdir(parents=True, exist_ok=True)

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
            "l1": L1Cache(l1_dir, self.config.cache),
            "l2": L2Index(l2_dir, self.config.cache),
            "sessions": SessionManager(data_dir, session_cfg),
            "vec": vec_store,
            "vec_mode": vec_mode,
            "trajectories": traj_store,
        }
        self._tenants[key] = tenant
        log.info(f"Tenant '{key}' initialized at {data_dir}")
        return tenant

    @property
    def active_tenants(self) -> list[str]:
        return list(self._tenants.keys())


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
    handler. memory_id-less HOT chunks dedup by (source, content).
    """
    merged: dict = {}
    for pass_chunks in passes:
        for c in pass_chunks:
            key = c.memory_id or ("__hot__", c.source, c.content)
            prev = merged.get(key)
            if prev is None or c.relevance > prev.relevance:
                merged[key] = c
    return list(merged.values())


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


def create_app(config: Optional[AgentBConfig] = None) -> FastAPI:
    if config is None:
        config = load_config()

    log.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

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
        log.info(f"⚡ Mnemo Cortex v4.5.0 — I remember everything so your agent doesn't have to.")
        log.info(f"  Reasoning: {reasoner.status}")
        log.info(f"  Embedding: {embedder.status}")
        log.info(f"  Data dir:  {config.data_dir}")
        log.info(f"  Agents:    {list(config.agents.keys()) or ['default']}")
        log.info(f"  Personas:  {list(config.personas.keys())}")
        log.info(f"  Live Wire: /ingest endpoint active — every exchange captured")
        asyncio.create_task(maintenance_loop())
        yield

    app = FastAPI(
        title="Mnemo Cortex",
        description="Drop-in memory superhero for AI agents",
        version="4.9.2",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=config.server.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

    # ── Body-size guard (DoS) ──
    # Reject oversized payloads before they get embedded, indexed, or written
    # to disk. Cheap header check — well-behaved clients send Content-Length.
    max_body = config.server.max_body_bytes
    if max_body > 0:
        @app.middleware("http")
        async def limit_body_size(request: Request, call_next):
            cl = request.headers.get("content-length")
            if cl is not None:
                try:
                    if int(cl) > max_body:
                        return Response(
                            f"Request body too large (limit {max_body} bytes)",
                            status_code=413,
                        )
                except ValueError:
                    return Response("Invalid Content-Length", status_code=400)
            return await call_next(request)

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
            if request.url.path == "/health":
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
            version="4.9.2",
            timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning={**reasoner.status, "healthy": r_ok},
            embedding={**embedder.status, "healthy": e_ok},
            agents_configured=list(config.agents.keys()) + tenants.active_tenants,
            default_persona="default",
            sessions=total_sessions,
            capture=gate.status(),
        )

    # ── Context ──
    @app.post("/context", response_model=ContextResponse)
    async def context(req: ContextRequest, request: Request):
        _enforce_scope(request, req.agent_id)
        start = time.time()
        persona = get_persona(config, req.persona, req.agent_id)
        tenant = tenants.get(req.agent_id)
        l1, l2 = tenant["l1"], tenant["l2"]
        memory_dir = tenant["memory_dir"]
        sessions = tenant["sessions"]
        vec_store: VecStore = tenant["vec"]

        # v3 filter setup. exclude_categories defaults to DEFAULT_HIDDEN_CATEGORIES
        # (session_log). Caller can opt back in by passing an explicit list — even
        # an empty one — to disable hiding.
        if req.exclude_categories is None:
            effective_exclude = set(DEFAULT_HIDDEN_CATEGORIES)
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
        pool_factor = 5 if req.mode == "explore" else 3
        overfetch = max(req.max_results * pool_factor, req.max_results + 5)

        cache_hits = {"HOT": 0, "L1": 0, "VEC": 0, "L2": 0, "L3": 0, "MEM0": 0}

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
        async def _retrieve_for_embedding(query_str: str, query_embedding) -> list[ContextChunk]:
            pass_chunks: list[ContextChunk] = []

            # HOT: keyword search over recent live sessions. These are raw
            # session logs, so they carry category=session_log and obey the same
            # two-tier default hiding as every other log (opt back in via
            # exclude_categories=[]). relevance 0.75: a keyword hit in a recent
            # session is decent signal, but the old hardcoded 0.95 put raw logs
            # above every semantic match, sight unseen.
            hot_results = sessions.search_hot(query_str, max_results=min(3, req.max_results))
            for hr in hot_results:
                content = f"[{hr['timestamp'][:16]}] User: {hr['prompt']}\nAgent: {hr['response']}"
                if hr.get("actions"):
                    content += "\nActions: " + " | ".join(hr["actions"][:3])
                if hr.get("thinking"):
                    content += f"\nThinking: {hr['thinking']}"
                c = ContextChunk(
                    content=content,
                    source=f"hot-session:{hr['session_id']}",
                    relevance=0.75,
                    cache_tier="HOT",
                    category="session_log",
                )
                if keep_chunk(c):
                    pass_chunks.append(c)

            # L1
            # v4.0.2: disk-truth the category before filtering — L1's cached
            # category is stale/absent after the reclassification migration, so
            # session_log leaked. Resolve per-hit (cheap, over-fetch is small),
            # like the VEC tier. v4.1: resolve_disk_truth returns None for
            # deleted memories.
            pass_chunks.extend(
                c for c in (resolve_disk_truth(c, memory_dir)
                            for c in l1.search(query_embedding, top_k=overfetch, persona=persona))
                if c is not None and keep_chunk(c)
            )

            # Intra-pass cross-tier dedup: a memory written via /writeback ends
            # up in BOTH the vec index and the L2/L3 stores. Without this, the
            # same chunk appears once per tier within this pass. (Cross-PASS
            # dedup — fusing the original query with expanded phrasings — happens
            # in merge_passes, by max-relevance, not first-wins.)
            seen_memory_ids: set[str] = {c.memory_id for c in pass_chunks if c.memory_id}

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
                        # vec0 distance is L2 by default; convert to similarity-ish (0..1)
                        relevance = 1.0 / (1.0 + hit.distance)
                        # v4.0.1: load category/source from the memory's JSON so the
                        # metadata filter (session_log exclusion, stale, source) applies
                        # to VEC hits too. Without this the VEC tier silently bypassed
                        # every category filter — session_log noise crowded recall.
                        category = provenance_source = None
                        age_days = stale = None
                        if hit.source_file and os.path.exists(hit.source_file):
                            try:
                                mj = json.loads(Path(hit.source_file).read_text())
                                category = mj.get("category")
                                provenance_source = mj.get("source")
                            except Exception:
                                pass
                        if hit.created_at:
                            age_days = (time.time() - hit.created_at) / 86400.0
                            if category:
                                stale = compute_stale_warning(category, hit.created_at)
                        c = ContextChunk(
                            content=hit.text,
                            source=f"memory:{hit.memory_id}",
                            relevance=relevance,
                            cache_tier="VEC",
                            memory_id=hit.memory_id,
                            provenance_source=provenance_source,
                            category=category,
                            age_days=age_days,
                            stale_warning=stale,
                        )
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

            # L2 (legacy read-only tier — new writes stopped in v4.1)
            # v4.0.2: resolve disk-truth category first — L2's metadata cache
            # kept the pre-migration category, so session_log leaked here too.
            # v4.1: resolve_disk_truth returns None for deleted memories.
            l2_results = [
                c for c in (resolve_disk_truth(c, memory_dir)
                            for c in l2.search(query_embedding, top_k=overfetch, persona=persona))
                if c is not None and keep_chunk(c)
                and (not c.memory_id or c.memory_id not in seen_memory_ids)
            ]
            pass_chunks.extend(l2_results)
            for c in l2_results:
                if c.memory_id:
                    seen_memory_ids.add(c.memory_id)

            # L3: the expensive disk-walk (embeds candidates) stays an escape
            # hatch — only runs when the cheap tiers couldn't fill the request.
            # #468 / v4.9.2: when ANY category filter was pushed into the kNN —
            # a pinned include OR the default session_log exclusion — and the
            # VEC tier actually served survivors, VEC (plus its escalation
            # retry) already did the filtered over-fetch that L3 would
            # duplicate. A partial result beats the multi-second disk-walk
            # (which on a large store exceeds the bridge timeout — the vec
            # search contract says the caller must NOT fall through). Gate on a
            # real VEC contribution, NOT just "filter set + index non-empty":
            # in the un-backfilled deploy window the category columns are NULL,
            # so include filters match nothing, exclusion filters drop nothing
            # in-index and keep_chunk then drops everything on disk-truth —
            # both leave zero VEC survivors, and there L3 is still the escape
            # hatch that finds the memories. Every VEC chunk that reached
            # pass_chunks already passed keep_chunk, so a survivor here is by
            # definition on-filter.
            vec_served_filtered = vec_filter_active and any(
                c.cache_tier == "VEC" for c in pass_chunks
            )
            if len(pass_chunks) < req.max_results and not vec_served_filtered:
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
        standard = await _retrieve_for_embedding(req.prompt, query_embedding)
        all_chunks = merge_passes([standard])

        # Thesaurus Loop (v4.2): escalate ONLY when the first pass whiffed. A
        # zero/weak result fans the query into a few alternative phrasings (one
        # isolated Flash call), searches each, and fuses by max-relevance. Good
        # searches never reach this branch, so there's no hot-path cost.
        exp = config.expansion
        if exp.enabled and req.expand is not False and not req.batch \
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
                extra_passes.append(await _retrieve_for_embedding(v, v_emb))
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
        if req.mode == "explore":
            # Serendipity lens: adjacency to the pool's top hit, no recency,
            # novelty over familiarity. Zero-scored chunks are the noise band
            # and must not pad the results.
            access = vec_store.access_counts([c.memory_id for c in all_chunks if c.memory_id])
            top_sim = max((c.relevance for c in all_chunks), default=0.0)
            scored = [
                (explore_score(
                    similarity=c.relevance,
                    top_similarity=top_sim,
                    category=c.category,
                    access_count=access.get(c.memory_id, 0) if c.memory_id else 0,
                ), c)
                for c in all_chunks
            ]
            all_chunks = [c for s, c in sorted(
                scored, key=lambda sc: sc[0], reverse=True) if s > 0.0]
        elif config.ranking.enabled:
            now = time.time()

            def _age(c: ContextChunk) -> Optional[float]:
                if c.age_days is not None:
                    return c.age_days
                if c.created_at:
                    return (now - float(c.created_at)) / 86400.0
                return None

            access = vec_store.access_counts([c.memory_id for c in all_chunks if c.memory_id])
            all_chunks.sort(
                key=lambda c: composite_score(
                    similarity=c.relevance,
                    age_days=_age(c),
                    category=c.category,
                    access_count=access.get(c.memory_id, 0) if c.memory_id else 0,
                    cfg=config.ranking,
                ),
                reverse=True,
            )

        selected = all_chunks[: req.max_results]
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
            agent_id=req.agent_id,
            persona=persona.name,
            provider_used=embedder.active_label,
        )

    # ── Preflight ──
    @app.post("/preflight", response_model=PreflightResponse)
    async def preflight(req: PreflightRequest):
        start = time.time()
        persona = get_persona(config, req.persona, req.agent_id)
        tenant = tenants.get(req.agent_id)
        l1, l2 = tenant["l1"], tenant["l2"]

        system_prompt = build_preflight_system_prompt(persona)

        user_prompt = f"USER'S PROMPT:\n{req.prompt}\n\nAGENT'S DRAFT RESPONSE:\n{req.draft_response}\n\nReview and provide your preflight verdict as JSON."

        # Cross-reference memory
        try:
            query_embedding = await embedder.embed(req.prompt, task_type="query")
            l1_hits = l1.search(query_embedding, top_k=2, persona=persona)
            l2_hits = l2.search(query_embedding, top_k=2, persona=persona)
            context_chunks = l1_hits + l2_hits
            if context_chunks:
                context_text = "\n\n".join(f"[{c.cache_tier}] {c.content}" for c in context_chunks)
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
                verdict=result.get("verdict", "PASS").upper(),
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
            return PreflightResponse(
                verdict="PASS", confidence=0.2,
                reason=f"AgentB couldn't validate — defaulting to PASS ({str(e)[:80]})",
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
        l1 = tenant["l1"]
        vec_store: VecStore = tenant["vec"]

        ts = req.timestamp or datetime.now(timezone.utc).isoformat()
        memory_id = hashlib.sha256(f"{req.session_id}:{ts}".encode()).hexdigest()[:16]

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
            "timestamp": ts, "created_at": time.time(),
            # v3 fields
            "source": source_used,
            "category": category_used,
            "additional_tags": additional_tags,
            "schema_version": 3,
        }
        # v4 provenance: how the category was decided, and whether the dreamer
        # should revisit it (regex fallback fired because the LLM was unavailable).
        if classified_by:
            memory_entry["classified_by"] = classified_by
        if needs_reclassification:
            memory_entry["needs_reclassification"] = True
        (memory_dir / f"{memory_id}.json").write_text(json.dumps(memory_entry, indent=2, default=str))
        log.info(f"Writeback: {req.session_id} → {memory_id} (agent: {req.agent_id or 'default'}, source={source_used}, category={category_used})")

        l1_updated = 0
        try:
            full_text = summary + "\n" + "\n".join(key_facts)
            embedding = await embedder.embed(full_text, use_breaker=not req.batch, task_type="document")
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
                    created_at=time.time(),
                    category=category_used,  # #468: mirror category into the search pre-filter column
                )
            except VecDimMismatch as e:
                # Dim mismatch is a configuration/contract bug, not a runtime
                # blip. Surface to the caller — silent vector loss is the
                # exact failure mode the dim guard was added to prevent.
                log.error(f"vec_index dim mismatch on writeback {memory_id}: {e}")
                raise HTTPException(500, f"vec index dim mismatch: {e}")

            for project in req.projects_referenced:
                pc = f"Project: {project}\nSession: {req.session_id}\nSummary: {summary}\n"
                pfacts = [f for f in key_facts if project.lower() in f.lower()]
                if pfacts:
                    pc += "Facts:\n" + "\n".join(f"- {f}" for f in pfacts)
                pe = await embedder.embed(pc, use_breaker=not req.batch, task_type="document")
                await l1.add(pc, f"project:{project}", pe)
                l1_updated += 1
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"Writeback indexing failed: {e}")

        return WritebackResponse(
            status="archived", memory_id=memory_id, agent_id=req.agent_id,
            l1_bundles_updated=l1_updated,
            message=f"Session {req.session_id} archived for agent '{req.agent_id or 'default'}'. {l1_updated} L1 bundles updated.",
            category_used=category_used,
            category_suggested=category_suggested_field,
            category_match_keywords=category_match_keywords_field,
            source_used=source_used,
            redactions=sum(red_counts.values()),
        )

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

        return IngestResponse(
            status="captured",
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
        entries = sessions.get_session_transcript(session_id)
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
    async def maintenance_loop():
        cycle = 0
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            cycle += 1
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

            for tenant_key, tenant in tenants._tenants.items():
                sessions = tenant["sessions"]
                memory_dir = tenant["memory_dir"]

                # Precache L1 bundles. use_breaker=False: this is background
                # batch work — it must not trip or be blocked by the breaker
                # that guards live /context (batch-vs-live isolation doctrine).
                try:
                    l1 = tenant["l1"]
                    recent = sorted(memory_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]
                    for mem_file in recent:
                        mem = json.loads(mem_file.read_text())
                        content = mem.get("summary", "")
                        if not content:
                            continue
                        bid = hashlib.sha256(content.encode()).hexdigest()[:12]
                        if bid in {b.get("id") for b in l1.bundles}:
                            continue
                        embedding = await embedder.embed(content, use_breaker=False, task_type="document")
                        mem_id = mem.get("id", mem_file.stem)
                        await l1.add(content, f"precache:{mem_id}", embedding,
                                     memory_id=mem_id, category=mem.get("category"))
                except Exception as e:
                    log.warning(f"Precache error for '{tenant_key}': {e}")

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
                    # in VEC — without this a session leaving the HOT tier
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
                                json.dumps(entry, indent=2, default=str))
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

    # ── Passport Lane (Phase 1) ──
    # Five MCP-facing routes under /passport/*. Self-contained: no shared state
    # with Mnemo's L1/L2/L3 cache. Data lives at $MNEMO_PASSPORT_DIR
    # (default ~/.mnemo/passport), auto-committed via git, never auto-pushed.
    from passport.api import router as passport_router
    app.include_router(passport_router)
    log.info("  Passport Lane: /passport/* endpoints active (5 tools)")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    port = int(os.environ["MNEMO_PORT"]) if os.environ.get("MNEMO_PORT") else cfg.server.port
    uvicorn.run("agentb.server:app", host=cfg.server.host, port=port,
                reload=False, log_level=cfg.log_level)
