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
import json
import time
import hashlib
import logging
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agentb.config import (
    load_config, AgentBConfig, get_agent_data_dir, get_persona, PersonaConfig,
)
from agentb.providers import create_resilient_reasoning, create_resilient_embedding
from agentb.cache import L1Cache, L2Index, l3_scan, ContextChunk, resolve_disk_truth
from agentb.sessions import SessionManager, SessionConfig
from agentb.provenance import (
    VALID_SOURCES, VALID_CATEGORIES, DEFAULT_HIDDEN_CATEGORIES,
    suggest_category, compute_stale_warning,
)
from agentb.classify import classify_category, reclassify_memory_dir
from agentb.vec import VecStore, detect_mode as vec_detect_mode, backfill as vec_backfill, VecDimMismatch
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


class ContextRequest(BaseModel):
    prompt: str = Field(..., description="The prompt to search context for")
    agent_id: Optional[str] = Field(None, description="Agent ID for tenant isolation")
    persona: Optional[str] = Field(None, description="Persona mode: default, strict, creative")
    max_results: int = Field(5, ge=1, le=20)
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
            "doctrine|incident|identity|relationship|decision|session_log|unknown."
        ),
    )
    exclude_categories: Optional[list[str]] = Field(
        None,
        description=(
            "Categories to hide. Defaults to DEFAULT_HIDDEN_CATEGORIES "
            "(session_log). Pass an empty list to disable hiding entirely."
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
            "identity|relationship|decision|session_log|unknown. If omitted, "
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

        tenant = {
            "data_dir": data_dir,
            "memory_dir": memory_dir,
            "l1": L1Cache(l1_dir, self.config.cache),
            "l2": L2Index(l2_dir, self.config.cache),
            "sessions": SessionManager(data_dir, session_cfg),
            "vec": vec_store,
            "vec_mode": vec_mode,
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

def create_app(config: Optional[AgentBConfig] = None) -> FastAPI:
    if config is None:
        config = load_config()

    log.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    reasoner = create_resilient_reasoning(config.reasoning)
    embedder = create_resilient_embedding(config.embedding)
    tenants = TenantManager(config)

    # Phase 3: shared global facts store (one file, all agents share)
    facts_path = Path(config.data_dir or os.path.expanduser("~/.agentb")) / "facts.sqlite"
    facts = FactsStore(facts_path)

    # Pre-initialize configured agents
    for agent_name in config.agents:
        tenants.get(agent_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(f"⚡ Mnemo Cortex v4.0.2 — I remember everything so your agent doesn't have to.")
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
        version="4.0.2",
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
    if config.server.auth_token:
        @app.middleware("http")
        async def check_auth(request: Request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            token = (request.headers.get("X-API-KEY") or
                     request.headers.get("Authorization", "").replace("Bearer ", ""))
            if token != config.server.auth_token:
                return Response("Unauthorized", status_code=401)
            return await call_next(request)

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
            version="4.0.2",
            timestamp=datetime.now(timezone.utc).isoformat(),
            reasoning={**reasoner.status, "healthy": r_ok},
            embedding={**embedder.status, "healthy": e_ok},
            agents_configured=list(config.agents.keys()) + tenants.active_tenants,
            default_persona="default",
            sessions=total_sessions,
        )

    # ── Context ──
    @app.post("/context", response_model=ContextResponse)
    async def context(req: ContextRequest):
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

        # Over-fetch so post-filter trims don't leave us short.
        overfetch = max(req.max_results * 3, req.max_results + 5)

        cache_hits = {"HOT": 0, "L1": 0, "VEC": 0, "L2": 0, "L3": 0, "MEM0": 0}
        all_chunks: list[ContextChunk] = []

        # HOT: Search recent session logs first (fastest, keyword matching)
        hot_results = sessions.search_hot(req.prompt, max_results=min(3, req.max_results))
        hot_chunks: list[ContextChunk] = []
        for hr in hot_results:
            content = f"[{hr['timestamp'][:16]}] User: {hr['prompt']}\nAgent: {hr['response']}"
            if hr.get("actions"):
                content += "\nActions: " + " | ".join(hr["actions"][:3])
            if hr.get("thinking"):
                content += f"\nThinking: {hr['thinking']}"
            hot_chunks.append(ContextChunk(
                content=content,
                source=f"hot-session:{hr['session_id']}",
                relevance=0.95,
                cache_tier="HOT",
            ))
        hot_kept = [c for c in hot_chunks if keep_chunk(c)][: req.max_results]
        all_chunks.extend(hot_kept)
        cache_hits["HOT"] = len(hot_kept)

        try:
            query_embedding = await embedder.embed(req.prompt)
        except Exception as e:
            raise HTTPException(503, f"Embedding unavailable: {e}")

        # L1
        remaining = req.max_results - len(all_chunks)
        if remaining > 0:
            # v4.0.2: disk-truth the category before filtering — L1's cached
            # category is stale/absent after the reclassification migration, so
            # session_log leaked. Resolve per-hit (cheap, over-fetch is small),
            # like the VEC tier, so the trim below counts only kept chunks.
            l1_results = [c for c in l1.search(query_embedding, top_k=overfetch, persona=persona)
                          if keep_chunk(resolve_disk_truth(c, memory_dir))]
            kept = l1_results[:remaining]
            all_chunks.extend(kept)
            cache_hits["L1"] = len(kept)

        # Cross-tier dedup: a memory written via /writeback ends up in BOTH
        # the vec index and the L2/L3 stores. Without this, the same chunk
        # appears once per tier and burns max_results budget.
        seen_memory_ids: set[str] = {c.memory_id for c in all_chunks if c.memory_id}

        # VEC: indexed sqlite-vec lookup over written memories
        remaining = req.max_results - len(all_chunks)
        if remaining > 0 and vec_store.count() > 0:
            try:
                vec_hits = vec_store.search(query_embedding, top_k=overfetch)
            except VecDimMismatch as e:
                log.error(f"vec query dim mismatch: {e}")
                vec_hits = []
            vec_chunks: list[ContextChunk] = []
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
                vec_chunks.append(ContextChunk(
                    content=hit.text,
                    source=f"memory:{hit.memory_id}",
                    relevance=relevance,
                    cache_tier="VEC",
                    memory_id=hit.memory_id,
                    provenance_source=provenance_source,
                    category=category,
                    age_days=age_days,
                    stale_warning=stale,
                ))
                seen_memory_ids.add(hit.memory_id)
            kept = [c for c in vec_chunks if keep_chunk(c)][:remaining]
            all_chunks.extend(kept)
            cache_hits["VEC"] = len(kept)

        # L2
        remaining = req.max_results - len(all_chunks)
        if remaining > 0:
            # v4.0.2: resolve disk-truth category first — L2's metadata cache
            # kept the pre-migration category, so session_log leaked here too.
            l2_results = [
                c for c in (resolve_disk_truth(c, memory_dir)
                            for c in l2.search(query_embedding, top_k=overfetch, persona=persona))
                if keep_chunk(c) and (not c.memory_id or c.memory_id not in seen_memory_ids)
            ]
            kept = l2_results[:remaining]
            all_chunks.extend(kept)
            cache_hits["L2"] = len(kept)
            for c in kept:
                if c.memory_id:
                    seen_memory_ids.add(c.memory_id)

        # L3
        remaining = req.max_results - len(all_chunks)
        if remaining > 0:
            l3_results = [
                c for c in await l3_scan(memory_dir, query_embedding,
                                          embed_fn=embedder.embed,
                                          threshold=config.cache.l3_similarity_threshold,
                                          top_k=overfetch,
                                          prefilter=passes_metadata)
                if keep_chunk(c) and (not c.memory_id or c.memory_id not in seen_memory_ids)
            ]
            kept = l3_results[:remaining]
            all_chunks.extend(kept)
            cache_hits["L3"] = len(kept)
            for c in kept:
                if c.memory_id:
                    seen_memory_ids.add(c.memory_id)

        latency = (time.time() - start) * 1000
        return ContextResponse(
            chunks=[ContextChunkResponse(**c.to_dict()) for c in all_chunks],
            total_found=len(all_chunks),
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
            query_embedding = await embedder.embed(req.prompt)
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
    async def writeback(req: WritebackRequest):
        # Check read-only
        if req.agent_id and req.agent_id in config.agents:
            if config.agents[req.agent_id].read_only:
                raise HTTPException(403, f"Agent '{req.agent_id}' is read-only")

        tenant = tenants.get(req.agent_id)
        memory_dir = tenant["memory_dir"]
        l1, l2 = tenant["l1"], tenant["l2"]
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
                reasoner, req.summary, req.key_facts,
                use_breaker=not req.batch,
                max_input_chars=config.classification.max_input_chars,
            )
            needs_reclassification = classified_by == "regex"
            category_suggested_field = category_used
            category_match_keywords_field = None
        else:
            suggestion_text = req.summary + "\n" + "\n".join(req.key_facts or [])
            category_used, suggestion_keywords = suggest_category(suggestion_text)
            classified_by = "regex"
            category_suggested_field = category_used
            category_match_keywords_field = suggestion_keywords
        additional_tags = req.additional_tags or []

        memory_entry = {
            "id": memory_id, "session_id": req.session_id,
            "agent_id": req.agent_id, "summary": req.summary,
            "key_facts": req.key_facts,
            "projects_referenced": req.projects_referenced,
            "decisions_made": req.decisions_made,
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
            full_text = req.summary + "\n" + "\n".join(req.key_facts)
            embedding = await embedder.embed(full_text, use_breaker=not req.batch)
            await l2.add(full_text, f"session:{req.session_id}", embedding,
                        metadata={
                            "projects": req.projects_referenced,
                            "decisions": req.decisions_made,
                            "agent_id": req.agent_id,
                            "memory_id": memory_id,
                            # v3 — needed for read-time stale_warning + filter logic
                            "provenance_source": source_used,
                            "category": category_used,
                            "additional_tags": additional_tags,
                        })

            try:
                vec_store.upsert(
                    memory_id,
                    full_text,
                    embedding,
                    source_file=(memory_dir / f"{memory_id}.json").as_posix(),
                    created_at=time.time(),
                )
            except VecDimMismatch as e:
                # Dim mismatch is a configuration/contract bug, not a runtime
                # blip. Surface to the caller — silent vector loss is the
                # exact failure mode the dim guard was added to prevent.
                log.error(f"vec_index dim mismatch on writeback {memory_id}: {e}")
                raise HTTPException(500, f"vec index dim mismatch: {e}")

            for project in req.projects_referenced:
                pc = f"Project: {project}\nSession: {req.session_id}\nSummary: {req.summary}\n"
                facts = [f for f in req.key_facts if project.lower() in f.lower()]
                if facts:
                    pc += "Facts:\n" + "\n".join(f"- {f}" for f in facts)
                pe = await embedder.embed(pc, use_breaker=not req.batch)
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

        tenant = tenants.get(req.agent_id)
        sessions = tenant["sessions"]

        result = sessions.ingest(
            prompt=req.prompt,
            response=req.response,
            metadata=req.metadata,
        )

        return IngestResponse(
            status="captured",
            session_id=result["session_id"],
            entry_number=result["entry_number"],
            agent_id=req.agent_id,
        )

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
            embedder.primary.embed,
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

            for tenant_key, tenant in tenants._tenants.items():
                # Precache L1 bundles
                try:
                    memory_dir = tenant["memory_dir"]
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
                        embedding = await embedder.embed(content)
                        mem_id = mem.get("id", mem_file.stem)
                        await l1.add(content, f"precache:{mem_id}", embedding,
                                     memory_id=mem_id, category=mem.get("category"))
                except Exception as e:
                    log.warning(f"Precache error for '{tenant_key}': {e}")

                # Archive expired hot sessions → warm
                try:
                    sessions = tenant["sessions"]
                    archived = sessions.archive_hot_sessions()
                    if archived:
                        log.info(f"Archived {len(archived)} hot sessions for '{tenant_key}'")
                        # Index archived summaries into L2
                        for arch in archived:
                            if arch.get("summary"):
                                try:
                                    emb = await embedder.embed(arch["summary"])
                                    l2 = tenant["l2"]
                                    await l2.add(arch["summary"],
                                                f"archived-session:{arch['session_id']}",
                                                emb,
                                                metadata={"key_facts": arch.get("key_facts", [])})
                                except Exception as e:
                                    log.warning(f"Failed to index archived session: {e}")
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
                        rstats = await reclassify_memory_dir(
                            tenant["memory_dir"], reasoner,
                            limit=config.classification.dreamer_max_per_cycle,
                            max_input_chars=config.classification.max_input_chars,
                            use_breaker=False,
                        )
                        if rstats["reclassified"]:
                            log.info(
                                f"Dreamer reclassified {rstats['reclassified']} memories "
                                f"for '{tenant_key}' ({rstats['by_category']})"
                            )
                    except Exception as e:
                        log.warning(f"Reclassification pass error for '{tenant_key}': {e}")

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
