#!/usr/bin/env python3
"""
Mnemo Dreaming — Nightly Cross-Agent Memory Synthesis
=====================================================
Reads the day's memories from ALL agents, synthesizes them into a single
brief, and writes that brief back into AgentB so every agent gets it at
startup.

Designed to run as a nightly cron job on the host carrying the memory
sources (typically the same machine running the Mnemo Cortex server).

Data sources:
  - AgentB writebacks: ~/.agentb/memory/<agent>/*.json (one dir per agent)
  - Mnemo v2 SQLite:   ~/.mnemo-v2/mnemo.sqlite3      (messages + summaries)

Agents are discovered automatically from the filesystem — every directory
under ~/.agentb/memory/ is treated as one agent's lane.

Output:
  - Writes dream brief to ~/.agentb/memory/dreamer/<dream-id>.json
  - Also writes human-readable markdown to ~/.agentb/dreams/YYYY-MM-DD.md

Usage:
  python3 mnemo-dream.py                  # Normal nightly run
  python3 mnemo-dream.py --dry-run        # Show what would be synthesized, don't write
  python3 mnemo-dream.py --hours 48       # Override time window (default: since last dream)
  python3 mnemo-dream.py --verbose        # Show all harvested memories before synthesis
"""

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AGENTB_DATA_DIR = Path(os.getenv("AGENTB_DATA_DIR", "~/.agentb")).expanduser()
MNEMO_DB_PATH = Path(os.getenv("MNEMO_DB_PATH", "~/.mnemo-v2/mnemo.sqlite3")).expanduser()
DREAM_DIR = AGENTB_DATA_DIR / "dreams"
AGENTS_ROOT = AGENTB_DATA_DIR / "agents"
DREAMER_MEMORY_DIR = AGENTS_ROOT / "dreamer" / "memory"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
DREAM_MODEL = os.getenv("MNEMO_DREAM_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Phase 3: facts extraction + contradiction notification config
MNEMO_URL = os.getenv("MNEMO_URL", "http://localhost:50001")
# Bus URL is optional — if unset/unreachable, contradiction notification gracefully
# logs and skips (best-effort). Set to a Tailscale URL when running cron on a
# remote host that needs to reach the busmaster on a different machine.
MNEMO_DREAM_BUS_URL = os.getenv("MNEMO_DREAM_BUS_URL", "")
# Bus-from agent name — must be a registered agent on the configured bus.
# Required if MNEMO_DREAM_BUS_URL is set. The bus dispatcher rejects
# envelopes whose "from" isn't in its agents registry.
MNEMO_DREAM_BUS_FROM = os.getenv("MNEMO_DREAM_BUS_FROM", "")
# Comma-separated list of bus targets to notify on contradictions. Required
# if MNEMO_DREAM_BUS_URL is set. Each must be a registered bus agent.
MNEMO_DREAM_BUS_TARGETS = [
    t.strip() for t in os.getenv("MNEMO_DREAM_BUS_TARGETS", "").split(",") if t.strip()
]
# Discord webhook is optional — direct human visibility for contradictions
# without waiting for an agent to surface them.
MNEMO_DREAM_DISCORD_WEBHOOK = os.getenv("MNEMO_DREAM_DISCORD_WEBHOOK", "")
# Disable Stage 0.5 extraction entirely (e.g. to debug pure-synthesis runs)
DREAM_SKIP_FACTS = os.getenv("DREAM_SKIP_FACTS", "").lower() in ("1", "true", "yes")

def _discover_agentb_agents() -> list[str]:
    """Discover real agents from ~/.agentb/agents/*/memory/ directories.

    Excludes 'dreamer' (output lane, would eat its own dreams) and any directory
    without a memory/ subdirectory (probe/test/empty dirs). Returns sorted list.
    """
    agents_root = AGENTB_DATA_DIR / "agents"
    if not agents_root.exists():
        return []
    discovered = []
    for d in agents_root.iterdir():
        if not d.is_dir() or d.name == "dreamer":
            continue
        if (d / "memory").is_dir():
            discovered.append(d.name)
    return sorted(discovered)


_pinned = os.getenv("MNEMO_DREAM_AGENTS", "").strip()
AGENTB_AGENTS = (
    [a.strip() for a in _pinned.split(",") if a.strip()]
    if _pinned
    else _discover_agentb_agents()
)

# Skip auto-capture noise — keep True to drop tool-call-flush summaries
# from harvest (the brief stays focused on intentional saves).
SKIP_AUTO_CAPTURE = False

log = logging.getLogger("mnemo-dream")

# ---------------------------------------------------------------------------
# Harvest: AgentB writebacks (one directory per agent)
# ---------------------------------------------------------------------------

def harvest_agentb(since: datetime) -> list[dict]:
    """Read AgentB writeback JSONs newer than `since` for AGENTB_AGENTS only.

    Post-2026-05-16 v2.10.0 cutover layout: memory files live at
    ~/.agentb/agents/<agent>/memory/*.json. Pre-cutover scripts looked at
    ~/.agentb/memory/<agent>/ — that path is empty post-cutover.
    """
    memories = []

    for agent_id in AGENTB_AGENTS:
        if agent_id == "dreamer":
            continue  # Don't eat our own dreams
        agent_memory_dir = AGENTS_ROOT / agent_id / "memory"
        if not agent_memory_dir.is_dir():
            log.warning(f"No memory dir for agent '{agent_id}' at {agent_memory_dir}")
            continue

        for f in agent_memory_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
                data = json.loads(f.read_text())
                memories.append({
                    "source": "agentb",
                    "agent_id": agent_id,
                    "session_id": data.get("session_id", "unknown"),
                    "timestamp": data.get("timestamp", mtime.isoformat()),
                    "summary": data.get("summary", ""),
                    "key_facts": data.get("key_facts", []),
                    "projects": data.get("projects_referenced", []),
                    "decisions": data.get("decisions_made", []),
                })
            except (json.JSONDecodeError, KeyError) as e:
                log.warning(f"Skipping {f}: {e}")

    return memories


# ---------------------------------------------------------------------------
# Harvest: Mnemo v2 SQLite (messages + summaries)
# ---------------------------------------------------------------------------

def harvest_mnemo_sqlite(since: datetime) -> list[dict]:
    """Read summaries and recent messages from the Mnemo v2 database."""
    if not MNEMO_DB_PATH.exists():
        log.warning(f"Mnemo DB not found at {MNEMO_DB_PATH}")
        return []

    conn = sqlite3.connect(str(MNEMO_DB_PATH))
    conn.row_factory = sqlite3.Row
    memories = []
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    # Get summaries created since cutoff
    for row in conn.execute("""
        SELECT c.agent_id, c.session_id, s.content, s.token_count,
               s.kind, s.depth, s.created_at
        FROM summaries s
        JOIN conversations c ON c.conversation_id = s.conversation_id
        WHERE s.created_at > ?
        ORDER BY s.created_at DESC
    """, (since_str,)):
        memories.append({
            "source": "mnemo-v2-summary",
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "timestamp": row["created_at"],
            "summary": row["content"],
            "key_facts": [],
            "projects": [],
            "decisions": [],
            "meta": f"{row['kind']} d{row['depth']} ({row['token_count']}tok)",
        })

    # Also get recent raw messages for context richness (hybrid approach)
    # Only assistant messages — they contain the substantive content
    for row in conn.execute("""
        SELECT c.agent_id, c.session_id, m.content, m.role, m.created_at
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE m.created_at > ? AND m.role = 'assistant'
        ORDER BY m.created_at DESC
        LIMIT 50
    """, (since_str,)):
        # Only include if substantive (>100 chars, not just tool acks)
        content = row["content"]
        if len(content) > 100:
            memories.append({
                "source": "mnemo-v2-message",
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "timestamp": row["created_at"],
                "summary": content[:2000],  # Cap individual messages
                "key_facts": [],
                "projects": [],
                "decisions": [],
            })

    conn.close()
    return memories


# ---------------------------------------------------------------------------
# Find last dream timestamp
# ---------------------------------------------------------------------------

def get_last_dream_time() -> datetime | None:
    """Find when the last dream was written."""
    if not DREAMER_MEMORY_DIR.exists():
        return None

    latest = None
    for f in DREAMER_MEMORY_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            ts = data.get("timestamp")
            if ts:
                dt = datetime.fromisoformat(ts)
                if latest is None or dt > latest:
                    latest = dt
        except (json.JSONDecodeError, ValueError):
            pass

    return latest


# ---------------------------------------------------------------------------
# Synthesize
# ---------------------------------------------------------------------------

DREAM_SYSTEM_PROMPT = """You are the memory synthesizer for a multi-agent workspace.

You'll be given the last period's memories from every agent in the workspace, grouped by agent_id. Each agent has its own role — you'll learn it from how they describe their own work.

Your job: produce a synthesis brief that answers:

1. **What was built or shipped** — specific deliverables, with file paths and versions where available
2. **What was decided** — choices made, directions set, approaches validated or rejected
3. **What's blocked or pending** — open issues, next steps, dependencies on people or external systems
4. **Cross-agent connections** — things one agent did that another should know about
5. **Lessons learned** — failures, workarounds, doctrines reinforced

Be specific. Names, paths, versions, error messages. No fluff, no filler. Every sentence should carry information.

Format as markdown with the sections above. Keep it dense but readable. This brief will be injected into each agent's startup context tomorrow morning."""

PER_AGENT_SYSTEM_PROMPT = """You are summarizing one agent's memories from a workspace day.

You'll be given that agent's writebacks in chronological order. Produce a dense, factual brief of what THIS agent did:

1. **Built/shipped** — deliverables with file paths, commits, versions
2. **Decided** — choices made, approaches validated or rejected
3. **Blocked or pending** — open issues, next steps
4. **Lessons learned** — failures, doctrines reinforced

Be specific. Names, paths, versions, error messages. No fluff. Output markdown, 8-20 bullet lines. Lead with the agent's name as a header."""

ROLLUP_SYSTEM_PROMPT = """You are the cross-agent memory synthesizer.

You'll be given per-agent daily briefs from a multi-agent workspace. Produce one joint synthesis that answers:

1. **What was built or shipped** — across all agents
2. **What was decided** — workspace-level choices
3. **What's blocked or pending** — open work, dependencies
4. **Cross-agent connections** — work one agent did that another should know about
5. **Lessons learned** — failures, workarounds, doctrines reinforced

Be specific. Names, paths, versions, error messages. No fluff. Format as markdown with the sections above. This brief will be injected into each agent's startup context."""


def _call_openrouter(system_prompt: str, user_content: str, max_tokens: int = 4096) -> tuple[str, dict]:
    """Single OpenRouter call. Returns (text, usage). Raises on non-200."""
    response = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/GuyMannDude/mnemo-cortex",
            "X-Title": "Mnemo Dreaming",
        },
        json={
            "model": DREAM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
        timeout=180.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:500]}")
    result = response.json()
    return result["choices"][0]["message"]["content"], result.get("usage", {})


def _build_agent_section(agent_id: str, agent_memories: list[dict]) -> str:
    """Format one agent's memories as a chronological brief for the LLM."""
    lines = [f"# Agent: {agent_id} ({len(agent_memories)} entries)"]
    for m in sorted(agent_memories, key=lambda x: x.get("timestamp", "")):
        ts = m.get("timestamp", "?")[:19]
        lines.append(f"\n## [{ts}] session={m.get('session_id', '?')}")
        lines.append(m["summary"])
        if m["key_facts"]:
            lines.append("Key facts:")
            for fact in m["key_facts"]:
                if fact != "auto_capture_flush":
                    lines.append(f"  - {fact}")
        if m.get("decisions"):
            lines.append("Decisions: " + "; ".join(m["decisions"]))
    return "\n".join(lines)


def synthesize(memories: list[dict], dry_run: bool = False) -> str:
    """Two-stage map-reduce synthesis.

    Stage 1 (map): each agent's memories → one per-agent brief.
    Stage 2 (reduce): per-agent briefs → one joint workspace brief.

    Bounds per-call token usage. Pre-fix the cron sent ~6M tokens to a 1M model
    nightly and 400'd every run since 2026-05-13.
    """
    by_agent: dict[str, list[dict]] = {}
    for m in memories:
        by_agent.setdefault(m["agent_id"], []).append(m)

    total_chars = sum(len(m["summary"]) for m in memories)
    log.info(f"Synthesis input: {len(memories)} memories from {len(by_agent)} agents, {total_chars:,} chars")

    if dry_run:
        return f"[DRY RUN] Would map-reduce {len(memories)} memories from {len(by_agent)} agents ({total_chars:,} chars)"

    if not OPENROUTER_API_KEY:
        log.error("No OPENROUTER_API_KEY set — cannot call LLM")
        sys.exit(1)

    # Stage 1: per-agent briefs
    per_agent_briefs: list[str] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    for agent_id in sorted(by_agent.keys()):
        agent_memories = by_agent[agent_id]
        section = _build_agent_section(agent_id, agent_memories)
        log.info(f"  stage 1 [{agent_id}]: {len(agent_memories)} entries, {len(section):,} chars")
        try:
            brief, usage = _call_openrouter(PER_AGENT_SYSTEM_PROMPT, section, max_tokens=2048)
        except RuntimeError as e:
            log.error(f"  stage 1 [{agent_id}] failed: {e}")
            sys.exit(1)
        per_agent_briefs.append(f"## Agent {agent_id} brief\n\n{brief}")
        total_prompt_tokens += usage.get("prompt_tokens", 0)
        total_completion_tokens += usage.get("completion_tokens", 0)

    # Stage 2: cross-agent rollup
    rollup_input = "# Per-agent briefs to synthesize\n\n" + "\n\n---\n\n".join(per_agent_briefs)
    log.info(f"  stage 2 rollup: {len(per_agent_briefs)} briefs, {len(rollup_input):,} chars")
    try:
        dream_text, usage = _call_openrouter(ROLLUP_SYSTEM_PROMPT, rollup_input, max_tokens=4096)
    except RuntimeError as e:
        log.error(f"  stage 2 failed: {e}")
        sys.exit(1)
    total_prompt_tokens += usage.get("prompt_tokens", 0)
    total_completion_tokens += usage.get("completion_tokens", 0)

    log.info(f"LLM usage total: {total_prompt_tokens} prompt, {total_completion_tokens} completion ({len(by_agent)+1} calls)")
    return dream_text


# ---------------------------------------------------------------------------
# Write dream
# ---------------------------------------------------------------------------

def write_dream(dream_text: str, memories: list[dict], since: datetime) -> str:
    """Write the dream to both AgentB memory and a readable markdown file."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    dream_id = hashlib.sha256(f"dream:{date_str}:{now.isoformat()}".encode()).hexdigest()[:16]

    # Ensure directories
    DREAMER_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    DREAM_DIR.mkdir(parents=True, exist_ok=True)

    # Count by agent
    agent_counts = {}
    for m in memories:
        a = m["agent_id"]
        agent_counts[a] = agent_counts.get(a, 0) + 1

    # Write AgentB-format JSON (so /writeback search finds it)
    memory_entry = {
        "id": dream_id,
        "session_id": f"dream-{date_str}",
        "agent_id": "dreamer",
        "summary": dream_text,
        "key_facts": [
            f"Dream covering {since.strftime('%Y-%m-%d %H:%M')} to {now.strftime('%Y-%m-%d %H:%M')} UTC",
            f"Sources: {', '.join(f'{a}({c})' for a, c in sorted(agent_counts.items()))}",
            f"Total memories synthesized: {len(memories)}",
        ],
        "projects_referenced": list({p for m in memories for p in m.get("projects", [])}),
        "decisions_made": list({d for m in memories for d in m.get("decisions", [])}),
        "timestamp": now.isoformat(),
        "created_at": time.time(),
    }
    json_path = DREAMER_MEMORY_DIR / f"{dream_id}.json"
    json_path.write_text(json.dumps(memory_entry, indent=2, default=str))

    # Write human-readable markdown
    md_content = f"""# Mnemo Dream — {date_str}

_Generated {now.strftime('%Y-%m-%d %H:%M UTC')} by mnemo-dream.py_
_Covering: {since.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%Y-%m-%d %H:%M')} UTC_
_Sources: {', '.join(f'{a} ({c} entries)' for a, c in sorted(agent_counts.items()))}_

---

{dream_text}
"""
    md_path = DREAM_DIR / f"{date_str}.md"
    md_path.write_text(md_content)

    log.info(f"Dream written: {json_path} + {md_path}")

    # Also POST through /writeback so the dream hits L2 index
    bridge_url = os.getenv("MNEMO_URL", "http://localhost:50001")
    try:
        wb_response = httpx.post(
            f"{bridge_url}/writeback",
            json={
                "session_id": f"dream-{date_str}",
                "agent_id": "dreamer",
                "summary": dream_text,
                "key_facts": memory_entry["key_facts"],
                "projects_referenced": memory_entry["projects_referenced"],
                "decisions_made": memory_entry["decisions_made"],
                # Batch writer: bypass the live embedder circuit breaker so a
                # large nightly dream can't trip or get blocked by it.
                "batch": True,
            },
            timeout=15.0,
        )
        if wb_response.status_code == 200:
            wb_data = wb_response.json()
            log.info(f"Dream synced to bridge (L2): memory_id={wb_data.get('memory_id', '?')}")
        else:
            log.warning(f"Bridge writeback returned {wb_response.status_code}")
    except Exception as e:
        log.warning(f"Bridge writeback failed (non-fatal): {e}")

    return dream_id


# ---------------------------------------------------------------------------
# Phase 3: Stage 0.5 — fact extraction + contradiction notification
# ---------------------------------------------------------------------------

FACT_EXTRACTION_SYSTEM_PROMPT = """You are extracting structured facts from agent memories. Each fact is a (entity, attribute, value) triple where:
- entity is a thing (person, machine, product, store, project)
- attribute is a property of that thing (location, owner, port, url, version)
- value is the current truth as stated

Rules:
1. CONSERVATIVE EXTRACTION ONLY. Extract facts that are stated DIRECTLY in the source text. Do NOT infer, do NOT bridge two statements into a third, do NOT promote possibilities into facts. If in doubt, skip.
2. Skip facts that change conversationally (mood, current task, "today we are doing X").
3. Skip facts that are too situational ("the cron ran at 7:05 today").
4. Skip facts that are clearly speculative ("might", "considering", "exploring", "could be").
5. Output JSON list. Empty list is valid AND COMMON — most memory batches will yield zero facts. That is correct.

Format: [{"entity": "...", "attribute": "...", "value": "...", "evidence_source": "memory:<id> — quoted snippet"}]

Output ONLY the JSON list, no preamble, no explanation."""


def extract_facts_for_agent(agent_id: str, agent_memories: list[dict]) -> list[dict]:
    """Stage 0.5: ask LLM to extract structured facts from one agent's memories.

    Returns parsed list of {entity, attribute, value, evidence_source} dicts.
    Empty list on extraction failure (logged, non-fatal).

    Filters auto-capture entries (they are tool-call logs, not stated facts).
    Synthesis stage still uses them — only extraction skips. This is structural,
    not a quality knob: auto-capture summaries don't contain the entity-attribute-
    value shape the prompt is trying to find.
    """
    if not agent_memories:
        return []
    # Filter auto-capture noise BEFORE building the section. Three flavors:
    #   1. Bridge captureCall flush — "[AUTO-CAPTURE] N tool calls:" prefix
    #   2. CC JSONL sync — session_id starts with "cc-jsonl-" + summary contains
    #      "session activity (auto-sync from JSONL"
    #   3. Generic auto pattern — session_id matches "<agent>-auto-<unixts>"
    def _is_auto_capture(m: dict) -> bool:
        summary = m.get("summary", "")
        sid = m.get("session_id", "")
        if summary[:50].startswith("[AUTO-CAPTURE]"):
            return True
        if "auto-sync from JSONL" in summary[:200]:
            return True
        if "auto_capture_flush" in (m.get("key_facts") or []):
            return True
        # session_id patterns: cc-auto-<ts>, opie-auto-<ts>, rocky-auto-<ts>, cc-jsonl-<uuid>
        if "-auto-" in sid or sid.startswith(("cc-jsonl-", "opie-jsonl-", "rocky-jsonl-")):
            return True
        return False

    extraction_memories = [m for m in agent_memories if not _is_auto_capture(m)]
    if not extraction_memories:
        log.info(f"  stage 0.5 [{agent_id}]: all {len(agent_memories)} memories are auto-capture noise; skipping")
        return []
    section = _build_agent_section(agent_id, extraction_memories)
    log.info(f"  stage 0.5 [{agent_id}]: extracting facts from {len(extraction_memories)}/{len(agent_memories)} entries (filtered auto-capture), {len(section):,} chars")
    try:
        raw, usage = _call_openrouter(FACT_EXTRACTION_SYSTEM_PROMPT, section, max_tokens=4096)
    except RuntimeError as e:
        log.error(f"  stage 0.5 [{agent_id}] LLM call failed: {e}")
        return []

    # Strip common LLM artifacts (markdown fences)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        facts = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning(f"  stage 0.5 [{agent_id}] JSON parse failed: {e}; first 200 chars: {cleaned[:200]}")
        return []
    if not isinstance(facts, list):
        log.warning(f"  stage 0.5 [{agent_id}] expected list, got {type(facts).__name__}")
        return []

    valid = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if not all(k in f and f[k] for k in ("entity", "attribute", "value")):
            continue
        valid.append({
            "entity": str(f["entity"]),
            "attribute": str(f["attribute"]),
            "value": str(f["value"]),
            "evidence_source": str(f.get("evidence_source", f"dream:{datetime.now(timezone.utc).strftime('%Y-%m-%d')} extraction")),
        })
    log.info(f"  stage 0.5 [{agent_id}] extracted {len(valid)} valid facts (LLM tokens: {usage.get('prompt_tokens', '?')} in / {usage.get('completion_tokens', '?')} out)")
    return valid


def post_facts(extracted: list[dict], source_agent: str) -> list[dict]:
    """POST each extracted fact to /facts. Returns the verified-vs-extracted
    contradictions (the cases the spec's notification flow needs to surface).

    A "verified-vs-extracted contradiction" is when:
      written=False AND was_contradiction=True AND previous_confidence='verified'
    i.e. Dreamer asserted high_probability against an existing verified fact and
    was rejected. Silently rejecting these is the exact pile-up risk Opie caught.
    """
    contradictions: list[dict] = []
    for fact in extracted:
        body = {
            "entity": fact["entity"],
            "attribute": fact["attribute"],
            "value": fact["value"],
            "confidence": "high_probability",  # Dreamer always asserts high_probability
            "evidence_source": fact["evidence_source"],
            "source_agent": source_agent,
        }
        try:
            resp = httpx.post(f"{MNEMO_URL}/facts", json=body, timeout=10.0)
            if resp.status_code != 200:
                log.warning(f"  /facts POST {resp.status_code} for {fact['entity']}/{fact['attribute']}: {resp.text[:200]}")
                continue
            data = resp.json()
            if (not data.get("written")) and data.get("was_contradiction") and data.get("previous_confidence") == "verified":
                contradictions.append({
                    "entity": fact["entity"],
                    "attribute": fact["attribute"],
                    "extracted_value": fact["value"],
                    "existing_verified_value": data.get("previous_value"),
                    "evidence_source": fact["evidence_source"],
                    "source_agent": source_agent,
                })
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            log.warning(f"  /facts POST exception for {fact['entity']}/{fact['attribute']}: {e}")
    return contradictions


def notify_contradictions(contradictions: list[dict], dream_date: str) -> None:
    """End-of-run batched notification. Best-effort to both bus + Discord;
    failures are logged, not raised. Quiet runs (no contradictions) produce
    no message."""
    if not contradictions:
        log.info("  no verified-vs-extracted contradictions this run")
        return
    log.warning(f"  {len(contradictions)} verified-vs-extracted contradiction(s) — notifying")

    summary_lines = [
        f"Dream {dream_date}: {len(contradictions)} verified-vs-extracted contradiction(s).",
        "Dreamer extracted facts that conflict with existing verified values.",
        "Existing verified values preserved; extracted values logged to fact_history.",
        "",
    ]
    for c in contradictions:
        summary_lines.append(
            f"  • {c['entity']}.{c['attribute']}: verified='{c['existing_verified_value']}' vs extracted='{c['extracted_value']}' (from {c['source_agent']}, {c['evidence_source']})"
        )
    summary_text = "\n".join(summary_lines)

    # Bus notification (optional, best-effort).
    # Requires MNEMO_DREAM_BUS_URL + MNEMO_DREAM_BUS_FROM + MNEMO_DREAM_BUS_TARGETS.
    # Envelope shape requires mesh_version + registered from/to per the disco-bus
    # dispatcher's validate_envelope_input. If a bus URL is set but the agent
    # names aren't, we log + skip — don't crash, don't silently pretend to send.
    if MNEMO_DREAM_BUS_URL:
        if not MNEMO_DREAM_BUS_FROM or not MNEMO_DREAM_BUS_TARGETS:
            log.warning(
                "  MNEMO_DREAM_BUS_URL is set but MNEMO_DREAM_BUS_FROM and/or "
                "MNEMO_DREAM_BUS_TARGETS are missing — skipping bus notification. "
                "Set both to enable (FROM must be a registered bus agent; TARGETS "
                "is comma-separated registered agent names)."
            )
        else:
            for target in MNEMO_DREAM_BUS_TARGETS:
                try:
                    envelope = {
                        "mesh_version": "0.5",
                        "from": MNEMO_DREAM_BUS_FROM,
                        "to": target,
                        "subject": f"dream-contradictions-{dream_date}",
                        "body": {
                            "source": "dreamer",
                            "summary": f"{len(contradictions)} verified-vs-extracted contradiction(s) this dream",
                            "dream_date": dream_date,
                            "contradictions": contradictions,
                            "guidance": "Verified facts preserved. Review each: was the verified fact wrong (use mnemo_fact_demote or assert new verified value), or was the extraction a false-positive (no action needed, drift signal)?",
                        },
                    }
                    r = httpx.post(f"{MNEMO_DREAM_BUS_URL}/mesh/ping", json=envelope, timeout=10.0)
                    if r.status_code in (200, 201, 202):
                        log.info(f"  bus notification → {target}: ok")
                    else:
                        log.warning(f"  bus notification → {target}: HTTP {r.status_code} {r.text[:200]}")
                except httpx.HTTPError as e:
                    log.warning(f"  bus notification → {target}: {e}")
    else:
        log.info("  MNEMO_DREAM_BUS_URL not set — skipping bus notification (set it to enable)")

    # Discord webhook (optional, best-effort)
    if MNEMO_DREAM_DISCORD_WEBHOOK:
        try:
            r = httpx.post(MNEMO_DREAM_DISCORD_WEBHOOK, json={"content": summary_text[:1900]}, timeout=10.0)
            if r.status_code in (200, 204):
                log.info(f"  discord webhook posted ({len(contradictions)} contradictions)")
            else:
                log.warning(f"  discord webhook returned {r.status_code}")
        except httpx.HTTPError as e:
            log.warning(f"  discord webhook failed: {e}")
    else:
        log.info("  MNEMO_DREAM_DISCORD_WEBHOOK not set — skipping discord (set it to enable)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Mnemo Dreaming — nightly cross-agent synthesis")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synthesized")
    parser.add_argument("--hours", type=int, default=0, help="Override: harvest last N hours")
    parser.add_argument("--verbose", action="store_true", help="Print all harvested memories")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[mnemo-dream] %(levelname)s %(message)s",
    )

    # Determine time window
    if args.hours > 0:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        log.info(f"Time window: last {args.hours} hours")
    else:
        last_dream = get_last_dream_time()
        if last_dream:
            since = last_dream
            log.info(f"Time window: since last dream at {last_dream.isoformat()}")
        else:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            log.info("No previous dream found — defaulting to last 24 hours")

    # Harvest from both data stores
    log.info("Harvesting AgentB writebacks...")
    agentb_memories = harvest_agentb(since)
    log.info(f"  Found {len(agentb_memories)} AgentB writebacks")

    log.info("Harvesting Mnemo v2 SQLite (messages + summaries)...")
    sqlite_memories = harvest_mnemo_sqlite(since)
    log.info(f"  Found {len(sqlite_memories)} Mnemo v2 entries")

    all_memories = agentb_memories + sqlite_memories

    if not all_memories:
        log.info("Nothing to dream about — no new memories since last dream.")
        return

    # Show what we found
    by_agent = {}
    for m in all_memories:
        a = m["agent_id"]
        by_agent[a] = by_agent.get(a, 0) + 1
    log.info(f"Total: {len(all_memories)} memories from {len(by_agent)} agents: {dict(sorted(by_agent.items()))}")

    if args.verbose:
        for m in sorted(all_memories, key=lambda x: x.get("timestamp", "")):
            print(f"\n[{m['agent_id']}] {m.get('timestamp', '?')[:19]}")
            print(f"  {m['summary'][:200]}...")
            if m["key_facts"]:
                for f in m["key_facts"]:
                    print(f"  * {f}")

    # Stage 0.5 — fact extraction (Phase 3). Runs before synthesis so the
    # extraction LLM cost is sunk before the bigger synthesis call, and so
    # contradictions surface in the same run that produced the brief.
    contradictions: list[dict] = []
    if not DREAM_SKIP_FACTS and not args.dry_run:
        log.info("Stage 0.5: extracting facts...")
        for agent_id in sorted(by_agent.keys()):
            agent_memories = [m for m in all_memories if m["agent_id"] == agent_id]
            extracted = extract_facts_for_agent(agent_id, agent_memories)
            if extracted:
                conflicts = post_facts(extracted, source_agent=agent_id)
                contradictions.extend(conflicts)
    elif DREAM_SKIP_FACTS:
        log.info("Stage 0.5 skipped (DREAM_SKIP_FACTS set)")

    # Synthesize
    log.info(f"Sending to {DREAM_MODEL} for synthesis...")
    dream_text = synthesize(all_memories, dry_run=args.dry_run)

    if args.dry_run:
        print(dream_text)
        return

    # Write
    dream_id = write_dream(dream_text, all_memories, since)
    log.info(f"Dream complete: id={dream_id}")

    # End-of-run contradiction notification (Phase 3). Best-effort; failures
    # logged not raised. Quiet runs produce no message.
    dream_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notify_contradictions(contradictions, dream_date)

    print(f"\n{'='*60}")
    print(f"DREAM COMPLETE — {dream_date}")
    print(f"{'='*60}")
    print(dream_text)


if __name__ == "__main__":
    main()
