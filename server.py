"""
Mnemo Cortex v0.4.0 — Drop-in Memory Superhero for AI Agents
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

import json
import time
import hashlib
import logging
import asyncio
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
from agentb.cache import L1Cache, L2Index, l3_scan, ContextChunk
from agentb.sessions import SessionManager, SessionConfig

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


class ContextChunkResponse(BaseModel):
    content: str
    source: str
    relevance: float
    cache_tier: str


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


class WritebackResponse(BaseModel):
    status: str
    memory_id: str
    agent_id: Optional[str]
    l1_bundles_updated: int
    message: str


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

        tenant = {
            "data_dir": data_dir,
            "memory_dir": memory_dir,
            "l1": L1Cache(l1_dir, self.config.cache),
            "l2": L2Index(l2_dir, self.config.cache),
            "sessions": SessionManager(data_dir, session_cfg),
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

    # Pre-initialize configured agents
    for agent_name in config.agents:
        tenants.get(agent_name)

    app = FastAPI(title="Mnemo Cortex", description="Drop-in memory superhero for AI agents", version="0.4.0")
    app.add_middleware(CORSMiddleware, allow_origins=config.server.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

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
            version="0.4.0",
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

        cache_hits = {"HOT": 0, "L1": 0, "L2": 0, "L3": 0}
        all_chunks: list[ContextChunk] = []

        # HOT: Search recent session logs first (fastest, keyword matching)
        hot_results = sessions.search_hot(req.prompt, max_results=min(3, req.max_results))
        for hr in hot_results:
            all_chunks.append(ContextChunk(
                content=f"[{hr['timestamp'][:16]}] User: {hr['prompt']}\nAgent: {hr['response']}",
                source=f"hot-session:{hr['session_id']}",
                relevance=0.95,  # hot data is highly relevant by recency
                cache_tier="HOT",
            ))
        cache_hits["HOT"] = len(hot_results)

        try:
            query_embedding = await embedder.embed(req.prompt)
        except Exception as e:
            raise HTTPException(503, f"Embedding unavailable: {e}")

        # L1
        remaining = req.max_results - len(all_chunks)
        if remaining > 0:
            l1_results = l1.search(query_embedding, top_k=remaining, persona=persona)
            all_chunks.extend(l1_results)
            cache_hits["L1"] = len(l1_results)

        # L2
        remaining = req.max_results - len(all_chunks)
        if remaining > 0:
            l2_results = l2.search(query_embedding, top_k=remaining, persona=persona)
            all_chunks.extend(l2_results)
            cache_hits["L2"] = len(l2_results)

        # L3
        remaining = req.max_results - len(all_chunks)
        if remaining > 0:
            l3_results = await l3_scan(memory_dir, query_embedding,
                                        embed_fn=embedder.embed,
                                        threshold=config.cache.l3_similarity_threshold,
                                        top_k=remaining)
            all_chunks.extend(l3_results)
            cache_hits["L3"] = len(l3_results)

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

        ts = req.timestamp or datetime.now(timezone.utc).isoformat()
        memory_id = hashlib.sha256(f"{req.session_id}:{ts}".encode()).hexdigest()[:16]

        memory_entry = {
            "id": memory_id, "session_id": req.session_id,
            "agent_id": req.agent_id, "summary": req.summary,
            "key_facts": req.key_facts,
            "projects_referenced": req.projects_referenced,
            "decisions_made": req.decisions_made,
            "timestamp": ts, "created_at": time.time(),
        }
        (memory_dir / f"{memory_id}.json").write_text(json.dumps(memory_entry, indent=2, default=str))
        log.info(f"Writeback: {req.session_id} → {memory_id} (agent: {req.agent_id or 'default'})")

        l1_updated = 0
        try:
            full_text = req.summary + "\n" + "\n".join(req.key_facts)
            embedding = await embedder.embed(full_text)
            await l2.add(full_text, f"session:{req.session_id}", embedding,
                        metadata={"projects": req.projects_referenced,
                                  "decisions": req.decisions_made, "agent_id": req.agent_id})

            for project in req.projects_referenced:
                pc = f"Project: {project}\nSession: {req.session_id}\nSummary: {req.summary}\n"
                facts = [f for f in req.key_facts if project.lower() in f.lower()]
                if facts:
                    pc += "Facts:\n" + "\n".join(f"- {f}" for f in facts)
                pe = await embedder.embed(pc)
                await l1.add(pc, f"project:{project}", pe)
                l1_updated += 1
        except Exception as e:
            log.error(f"Writeback indexing failed: {e}")

        return WritebackResponse(
            status="archived", memory_id=memory_id, agent_id=req.agent_id,
            l1_bundles_updated=l1_updated,
            message=f"Session {req.session_id} archived for agent '{req.agent_id or 'default'}'. {l1_updated} L1 bundles updated.",
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

    # ── Background: precache + session archival ──
    async def maintenance_loop():
        while True:
            await asyncio.sleep(300)  # every 5 minutes

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
                        await l1.add(content, f"precache:{mem.get('id', mem_file.stem)}", embedding)
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

    @app.on_event("startup")
    async def startup():
        log.info(f"⚡ Mnemo Cortex v0.4.0 — I remember everything so your agent doesn't have to.")
        log.info(f"  Reasoning: {reasoner.status}")
        log.info(f"  Embedding: {embedder.status}")
        log.info(f"  Data dir:  {config.data_dir}")
        log.info(f"  Agents:    {list(config.agents.keys()) or ['default']}")
        log.info(f"  Personas:  {list(config.personas.keys())}")
        log.info(f"  Live Wire: /ingest endpoint active — every exchange captured")
        asyncio.create_task(maintenance_loop())

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    uvicorn.run("agentb.server:app", host=cfg.server.host, port=cfg.server.port,
                reload=False, log_level=cfg.log_level)
