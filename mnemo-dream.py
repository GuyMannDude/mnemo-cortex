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

# Windows redirected-stdout safety (mirrors cli.py issue #3): under a Scheduled
# Task the dreamer's stdout is not a console and defaults to cp1252, which can't
# encode '→'/'✅'/emoji in the dream text or git-sync block — the final print()
# crashed the run with exit 1 *after* the brief + writeback had already
# succeeded, falsely signalling failure. Reconfigure to utf-8/replace; no-op on
# a normal terminal and on platforms that already default to utf-8.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

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


def _mnemo_auth_headers() -> dict:
    """Mnemo API token: env MNEMO_AUTH_TOKEN first (the cron sources
    agentb-bridge.env), else ~/.mnemo-auth-token (mode 0600). Sent as X-API-KEY
    so the Dreamer authenticates once the server enforces auth; ignored before."""
    tok = os.getenv("MNEMO_AUTH_TOKEN", "").strip()
    if not tok:
        try:
            tok = (Path.home() / ".mnemo-auth-token").read_text(encoding="utf-8").strip()
        except OSError:
            tok = ""
    return {"X-API-KEY": tok} if tok else {}


MNEMO_AUTH_HEADERS = _mnemo_auth_headers()
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

# Cap per-agent section size. One high-volume agent (opie's auto-capture has hit
# ~19MB / ~4.9M tokens) alone exceeds the synthesis model's 1M-token context
# window and 400s the whole run — the per-agent map-reduce split isn't enough
# when a SINGLE agent overflows. Recency-first: a "since last dream" synthesis
# cares most about the newest entries, so the oldest are dropped first. Env-
# overridable. 1M chars (cc's ~1.06M section synthesized fine; opie's 2.5M got a
# provider-side 400 wrapped in a 200) keeps the call reliably inside the provider's
# real limit, with the adaptive-halving retry as the token-density backstop.
MAX_AGENT_SECTION_CHARS = int(os.getenv("MNEMO_DREAM_MAX_AGENT_SECTION_CHARS", "1000000"))
# Stage 0.5 fact extraction is chunked so each LLM call's OUTPUT (a JSON fact
# array) stays within max_tokens. One big batch (e.g. cc's 165-entry / 64K-char
# day on 2026-06-13) overruns the output cap, truncates mid-string, and fails
# json.loads. Chunking by input chars bounds the output array per call.
# Env-overridable.
FACT_EXTRACTION_CHUNK_CHARS = int(os.getenv("MNEMO_DREAM_FACT_CHUNK_CHARS", "20000"))
# Output ceiling for a fact-extraction call. The 2026-06-14 verify showed 20K-char
# input chunks STILL overran a 4096-token output (truncated at output char ~10-13K),
# so the v4.2.2 chunking assumption ("fits well inside max_tokens") was too optimistic.
# 8192 gives the fact array room to finish; the salvage parser recovers anything that
# still truncates. Facts are cheap output — headroom here costs little.
FACT_EXTRACTION_MAX_TOKENS = int(os.getenv("MNEMO_DREAM_FACT_MAX_TOKENS", "8192"))

# Stage 0.7 — trajectory strategy distillation (v4.7, Trajectory Phase 2, Opie
# bus #995). Opt-in and default OFF like the Developer Dump: set
# MNEMO_DREAM_STRATEGIES=1 to enable. --dry-run runs the distillation and
# prints the items but never posts them (the human quality gate).
MNEMO_DREAM_STRATEGIES = os.getenv("MNEMO_DREAM_STRATEGIES", "").lower() in ("1", "true", "yes")
# One session's stream goes to the LLM WHOLE — task segmentation happens inside
# the call (bus #995: boundaries are context switches, and pre-chunking would
# split tasks mid-stream into useless fragments). Oversize sessions keep the
# newest entries, announced loudly, same recency-first rule as synthesis.
STRATEGY_SESSION_MAX_CHARS = int(os.getenv("MNEMO_DREAM_STRATEGY_SESSION_MAX_CHARS", "400000"))
# 16384 not 8192: Stage 0.5 already learned dense output overruns 8192 and
# truncates mid-array (v4.2.3); strategy items with step lists are denser still.
STRATEGY_MAX_TOKENS = int(os.getenv("MNEMO_DREAM_STRATEGY_MAX_TOKENS", "16384"))
# Sessions with fewer sync batches than this are slivers with no trajectory to
# judge; skipped with a log line (never silently).
STRATEGY_MIN_BATCHES = int(os.getenv("MNEMO_DREAM_STRATEGY_MIN_BATCHES", "3"))
# Curation window (bus #995): a trajectory with no save/recall activity for
# this many days is FLAGGED in the dream report for review. Flag-only —
# consolidate/merge/demote stays a Dreamer-judgment phase once flagged data
# actually exists; nothing is ever auto-deleted.
STRATEGY_STALE_DAYS = float(os.getenv("MNEMO_DREAM_STRATEGY_STALE_DAYS", "90"))
# Cap on the intentional-saves narrative block appended to each session stream.
STRATEGY_NARRATIVE_MAX_CHARS = int(os.getenv("MNEMO_DREAM_STRATEGY_NARRATIVE_MAX_CHARS", "30000"))

# Git-sync wedge (opt-in). When enabled, the Dreamer reports whether its watched
# git checkouts have uncommitted changes, unpushed commits, or are behind their
# remote — catching the "edited a repo, forgot to push, next machine pulls stale"
# drift the recall-side facts seeder can't see. Independent of the (unbuilt) full
# Dreaming-Brain reconciliation stage. Each clone only sees its OWN local state,
# so for full coverage of a multi-machine setup run the check on each editing host.
MNEMO_DREAM_GIT_SYNC_CHECK = os.getenv("MNEMO_DREAM_GIT_SYNC_CHECK", "").lower() in ("1", "true", "yes")

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
                data = json.loads(f.read_text(encoding="utf-8"))
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
            data = json.loads(f.read_text(encoding="utf-8"))
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
    # OpenRouter can return HTTP 200 with an error/empty body (provider error,
    # moderation, output-too-long, transient upstream). Validate before indexing
    # so this surfaces as a catchable RuntimeError — not a raw KeyError that
    # escapes the per-stage handlers and crashes the whole run.
    if not isinstance(result.get("choices"), list) or not result["choices"]:
        err = result.get("error", result)
        raise RuntimeError(f"OpenRouter 200 but no choices: {json.dumps(err)[:500]}")
    return result["choices"][0]["message"]["content"], result.get("usage", {})


def _call_openrouter_adaptive(
    system_prompt: str, user_content: str, max_tokens: int = 4096, min_chars: int = 20000
) -> tuple[str, dict]:
    """_call_openrouter, but halve the input and retry on a context-length 400.

    The per-agent char cap (MAX_AGENT_SECTION_CHARS) prevents overflow in the
    normal case; this is the belt-and-suspenders for token-density spikes —
    path/UUID/hash-dense content tokenizes far denser than a char estimate
    predicts (the adaptive-truncation doctrine). Keeps the TAIL on each halving
    (sections are chronological, so the tail is the most recent content).
    """
    content = user_content
    while True:
        try:
            return _call_openrouter(system_prompt, content, max_tokens=max_tokens)
        except RuntimeError as e:
            msg = str(e).lower()
            # Size-related failures worth retrying smaller: explicit context-length
            # errors, plus the provider-side 400 that OpenRouter wraps in a 200
            # ("no choices" / "provider returned error", code 400) — a large opie
            # section hits the latter before the former.
            is_oversize = (
                "context length" in msg or "maximum context" in msg or "context_length" in msg
                or "no choices" in msg or "provider returned error" in msg
            )
            if is_oversize and len(content) > min_chars:
                new_len = max(min_chars, len(content) // 2)
                log.warning(
                    f"  context-length 400 at {len(content):,} chars; retrying at "
                    f"{new_len:,} (keeping most recent)"
                )
                content = content[-new_len:]
                continue
            raise


def _render_memory(m: dict) -> str:
    """Format one memory entry as a chronological block for the LLM."""
    ts = m.get("timestamp", "?")[:19]
    parts = [f"\n## [{ts}] session={m.get('session_id', '?')}", m["summary"]]
    if m["key_facts"]:
        parts.append("Key facts:")
        for fact in m["key_facts"]:
            if fact != "auto_capture_flush":
                parts.append(f"  - {fact}")
    if m.get("decisions"):
        parts.append("Decisions: " + "; ".join(m["decisions"]))
    return "\n".join(parts)


def _build_agent_section(agent_id: str, agent_memories: list[dict]) -> str:
    """Format one agent's memories as a chronological brief for the LLM.

    Capped at MAX_AGENT_SECTION_CHARS, keeping the MOST RECENT entries — a single
    high-volume agent (opie's auto-capture has reached ~19MB) would otherwise
    blow past the model's context window and 400 the whole run. Kept entries are
    still rendered chronologically. The drop is announced in the header and
    logged (never a silent truncation).
    """
    ordered = sorted(agent_memories, key=lambda x: x.get("timestamp", ""))
    rendered = [(m, _render_memory(m)) for m in ordered]
    total = sum(len(r) for _, r in rendered)

    dropped = 0
    if total > MAX_AGENT_SECTION_CHARS:
        # Drop oldest-first until under budget (keep the newest entries).
        kept_rev: list[tuple[dict, str]] = []
        budget = MAX_AGENT_SECTION_CHARS
        for m, r in reversed(rendered):
            if budget - len(r) < 0:
                break
            kept_rev.append((m, r))
            budget -= len(r)
        dropped = len(rendered) - len(kept_rev)
        rendered = list(reversed(kept_rev))
        log.warning(
            f"  [{agent_id}] section {total:,} chars > {MAX_AGENT_SECTION_CHARS:,} cap — "
            f"dropped {dropped} oldest of {len(ordered)} entries (kept newest {len(rendered)})"
        )

    header = f"# Agent: {agent_id} ({len(rendered)} entries"
    if dropped:
        header += f"; {dropped} older entries omitted to fit the {MAX_AGENT_SECTION_CHARS:,}-char cap"
    header += ")"
    return "\n".join([header] + [r for _, r in rendered])


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
            brief, usage = _call_openrouter_adaptive(PER_AGENT_SYSTEM_PROMPT, section, max_tokens=2048)
        except RuntimeError as e:
            # Isolate per-agent failures: one agent's LLM error must not abort the
            # whole run and suppress the notification that the OTHER agents' good
            # dreams should fire. Skip this agent, keep going. (Pre-fix this was
            # sys.exit(1) — opie's 19MB section 400'd and killed every run since.)
            log.error(f"  stage 1 [{agent_id}] failed, skipping this agent: {e}")
            continue
        per_agent_briefs.append(f"## Agent {agent_id} brief\n\n{brief}")
        total_prompt_tokens += usage.get("prompt_tokens", 0)
        total_completion_tokens += usage.get("completion_tokens", 0)

    if not per_agent_briefs:
        log.error("  stage 1: every agent failed — no briefs to roll up, aborting run")
        sys.exit(1)

    # Stage 2: cross-agent rollup
    rollup_input = "# Per-agent briefs to synthesize\n\n" + "\n\n---\n\n".join(per_agent_briefs)
    log.info(f"  stage 2 rollup: {len(per_agent_briefs)} briefs, {len(rollup_input):,} chars")
    try:
        dream_text, usage = _call_openrouter_adaptive(ROLLUP_SYSTEM_PROMPT, rollup_input, max_tokens=4096)
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
    # v4.1: the brief is LLM output synthesized from session content — run it
    # through the same redaction choke point as every other write. The dream
    # markdown lands on disk and gets injected into agent startup context; a
    # key that slipped into a writeback before redaction shipped must not be
    # amplified into every agent's morning brief.
    try:
        from agentb.redact import redact_text
        dream_text, red_counts = redact_text(dream_text)
        if red_counts:
            log.warning(f"🔒 Redacted {sum(red_counts.values())} secret(s) from dream brief: "
                        + ", ".join(f"{k}×{v}" for k, v in red_counts.items()))
    except ImportError:
        log.warning("agentb.redact unavailable — dream brief written unredacted")

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
    json_path.write_text(json.dumps(memory_entry, indent=2, default=str), encoding="utf-8")

    # Write human-readable markdown
    md_content = f"""# Mnemo Dream — {date_str}

_Generated {now.strftime('%Y-%m-%d %H:%M UTC')} by mnemo-dream.py_
_Covering: {since.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%Y-%m-%d %H:%M')} UTC_
_Sources: {', '.join(f'{a} ({c} entries)' for a, c in sorted(agent_counts.items()))}_

---

{dream_text}
"""
    md_path = DREAM_DIR / f"{date_str}.md"
    md_path.write_text(md_content, encoding="utf-8")

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
            headers=MNEMO_AUTH_HEADERS,
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


def _is_auto_capture(m: dict) -> bool:
    """Ambient auto-capture noise, as opposed to intentional saves. Three flavors:
      1. Bridge captureCall flush — "[AUTO-CAPTURE] N tool calls:" prefix
      2. CC JSONL sync — session_id starts with "cc-jsonl-" + summary contains
         "session activity (auto-sync from JSONL"
      3. Generic auto pattern — session_id matches "<agent>-auto-<unixts>"
    Stage 0.5 SKIPS these (no entity-attribute-value shape to extract);
    Stage 0.7 SELECTS the jsonl-sync flavor (they ARE the session streams).
    """
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


def _chunk_memories_by_chars(memories: list[dict], budget: int) -> list[list[dict]]:
    """Split chronologically-ordered memories into chunks whose rendered size
    stays within `budget` chars, so each fact-extraction call's OUTPUT array fits
    inside max_tokens. A single oversized memory becomes its own chunk (the
    per-call adaptive halving handles a giant one); never a silent drop here.
    """
    ordered = sorted(memories, key=lambda x: x.get("timestamp", ""))
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for m in ordered:
        r = len(_render_memory(m))
        if cur and cur_chars + r > budget:
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append(m)
        cur_chars += r
    if cur:
        chunks.append(cur)
    return chunks


def _parse_fact_array(cleaned: str) -> tuple[list, bool]:
    """Parse a JSON array of fact objects, salvaging a truncated tail.

    The fact array is the LLM's OUTPUT; on a heavy chunk it can exceed the
    output-token ceiling and arrive truncated mid-object ("Unterminated string",
    "Expecting property name") — the 2026-06-14 failure mode. A plain json.loads
    throws away EVERY fact in such an array, including the complete ones before the
    cut. So: try a clean parse first; if that fails, walk complete top-level objects
    out of the (possibly truncated) array with raw_decode and keep what parsed.

    Returns (facts, salvaged): salvaged=False on a clean parse, True when we fell
    back to object-by-object recovery (so the caller can log it).

    strict=False throughout: LLMs emit raw newlines/tabs INSIDE string values
    (multi-line lesson text, quoted shell snippets), which strict json rejects
    ("Invalid control character") — the 2026-07-02 Stage 0.7 first-live-run
    failure, where the defect was in the FIRST object so salvage got nothing.
    """
    try:
        data = json.loads(cleaned, strict=False)
        if isinstance(data, list):
            return data, False
        # A bare object instead of an array — wrap it so one fact isn't lost.
        if isinstance(data, dict):
            return [data], False
    except json.JSONDecodeError:
        pass

    # Salvage path: greedily decode complete objects out of the array. Anchor on a
    # '[' that actually begins a decodable array — LLM preamble can carry a stray
    # bracket ("Facts [extracted]:") that would otherwise mis-anchor the scan; if a
    # candidate '[' yields nothing, advance to the next one. Within the real array we
    # stop at the first decode failure (the truncation point), keeping all complete
    # objects before it.
    salvaged: list = []
    decoder = json.JSONDecoder(strict=False)
    n = len(cleaned)
    search = 0
    while True:
        start = cleaned.find("[", search)
        if start == -1:
            return salvaged, True
        i = start + 1
        while i < n:
            while i < n and cleaned[i] in " \t\r\n,":
                i += 1
            if i >= n or cleaned[i] == "]":
                break
            try:
                obj, end = decoder.raw_decode(cleaned, i)
            except json.JSONDecodeError:
                break  # truncated mid-object — stop, keep the complete ones before it
            salvaged.append(obj)
            i = end
        if salvaged:
            return salvaged, True
        search = start + 1  # this '[' was a stray bracket in prose; try the next


def _extract_facts_from_section(agent_id: str, section: str, label: str = "") -> list[dict] | None:
    """One fact-extraction LLM call for a single section. Returns the validated
    fact list, or None if the call or JSON parse failed. Pulled out of
    extract_facts_for_agent so a parse failure costs ONE chunk, not the whole
    agent's facts (the bug that lost all 585 cc entries' facts on 2026-06-13).
    """
    try:
        raw, _ = _call_openrouter_adaptive(
            FACT_EXTRACTION_SYSTEM_PROMPT, section, max_tokens=FACT_EXTRACTION_MAX_TOKENS
        )
    except RuntimeError as e:
        log.error(f"  stage 0.5 [{agent_id}]{label} LLM call failed: {e}")
        return None

    # Strip common LLM artifacts (markdown fences)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    facts, salvaged = _parse_fact_array(cleaned)
    if salvaged:
        if facts:
            log.warning(
                f"  stage 0.5 [{agent_id}]{label} JSON incomplete (likely output truncation) "
                f"— salvaged {len(facts)} complete fact object(s) from the array"
            )
        else:
            log.warning(
                f"  stage 0.5 [{agent_id}]{label} JSON parse failed, nothing salvageable; "
                f"first 200 chars: {cleaned[:200]}"
            )
            return None

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
    return valid


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
    extraction_memories = [m for m in agent_memories if not _is_auto_capture(m)]
    if not extraction_memories:
        log.info(f"  stage 0.5 [{agent_id}]: all {len(agent_memories)} memories are auto-capture noise; skipping")
        return []
    chunks = _chunk_memories_by_chars(extraction_memories, FACT_EXTRACTION_CHUNK_CHARS)
    total_chars = sum(len(_render_memory(m)) for m in extraction_memories)
    log.info(
        f"  stage 0.5 [{agent_id}]: extracting facts from {len(extraction_memories)}/{len(agent_memories)} "
        f"entries (filtered auto-capture), {total_chars:,} chars in {len(chunks)} chunk(s)"
    )

    all_valid: list[dict] = []
    failed = 0
    for i, chunk in enumerate(chunks):
        label = f" chunk {i + 1}/{len(chunks)}" if len(chunks) > 1 else ""
        # Never-silent: a lone entry over the budget can't be split further here;
        # _call_openrouter_adaptive may halve (and drop the tail of) its input on a
        # context-400, so facts in that dropped tail are lost. Surface it.
        if len(chunk) == 1 and (only_chars := len(_render_memory(chunk[0]))) > FACT_EXTRACTION_CHUNK_CHARS:
            log.warning(
                f"  stage 0.5 [{agent_id}]{label} single entry is {only_chars:,} chars "
                f"(> {FACT_EXTRACTION_CHUNK_CHARS:,} budget) — adaptive halving may truncate its tail; "
                f"facts in any dropped tail will be lost"
            )
        section = _build_agent_section(agent_id, chunk)
        result = _extract_facts_from_section(agent_id, section, label)
        if result is None:
            failed += 1
            continue
        all_valid.extend(result)

    if failed:
        log.warning(
            f"  stage 0.5 [{agent_id}] {failed}/{len(chunks)} chunk(s) failed extraction — "
            f"kept facts from the {len(chunks) - failed} that parsed (one bad chunk no longer drops the whole agent)"
        )
    log.info(f"  stage 0.5 [{agent_id}] extracted {len(all_valid)} valid facts across {len(chunks) - failed}/{len(chunks)} chunk(s)")
    return all_valid


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
            resp = httpx.post(f"{MNEMO_URL}/facts", json=body, headers=MNEMO_AUTH_HEADERS, timeout=10.0)
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


# ---------------------------------------------------------------------------
# Trajectory Phase 2: Stage 0.7 — strategy distillation (v4.7, Opie bus #995)
# ---------------------------------------------------------------------------
# Source of truth pivot (bus #996): the Developer Dump only sees Mnemo-bridge
# tool calls, so it cannot reconstruct task execution. The trajectory substrate
# is the jsonl-sync session stream — ordered tool sequences + the agent's own
# narrated turns, already landing server-side as session_log writebacks and
# grouped by a real session_id. Distilled items are stored in the Phase-1
# trajectory store (source="dreamer") and recalled via mnemo_recall_trajectory
# — no new retrieval infrastructure.

STRATEGY_DISTILL_SYSTEM_PROMPT = """You are distilling reusable task strategies from one agent's work-session activity stream.

Input: one session in chronological order — activity batches carrying turn snippets and tool-call sequences ([tool: Bash] etc.), optionally followed by a NARRATIVE CONTEXT block of the agent's own intentional memory saves from the same period (these carry task outcomes and the agent's own stated lessons).

Step 1 — SEGMENT the session into tasks. A task boundary is a context switch: new topic, new file, new problem. A trajectory is the story of ONE task from start to finish: what was attempted, what failed, what worked, the final approach. Never treat a session as one task if it clearly switches contexts.

Step 2 — JUDGE each task ruthlessly. A strategy item is justified ONLY when a future agent facing the same task class would act DIFFERENTLY for having read it. Hunt failures first: errors, retries, dead ends, and recoveries in the stream are where the transferable lessons live ("X breaks when Y; do Z instead"). A clean success earns an item only when the recipe is genuinely non-obvious.

One exception to the clean-success bar: AESTHETIC TECHNIQUES. When the stream shows a creative choice shaping visual or artistic output — an approach or parameter picked over an alternative for how the result LOOKS, iterated against visual judgment, or explicitly kept/praised by the user — that technique is distillable even though nothing failed. The bar is evidence of aesthetic CHOICE: cleanly executing an art pipeline is NOT a technique; the choice inside it is. Phrase it as transferable advice like any other item.

NEVER emit items for: routine rituals (session startup/wrap-up, standard commits, memory writebacks), ordinary documentation or config edits, one-off trivia, or any task whose only lesson is "it worked when done carefully."

Output ONLY a JSON array. ZERO items is the NORMAL result for most sessions; more than 3 items from one session is almost always over-extraction.
[{
  "task_type": "kebab-case-task-class",
  "task_description": "one line, phrased as the situation a future agent would search for",
  "steps": [{"action": "what to do", "tool_used": "tool/command or null", "result_summary": "what it produces"}],
  "outcome": "the distilled lesson, phrased as advice: 'When Y, do X because Z'",
  "rating": 1-5,
  "derived_from": "success" or "failure",
  "evidence": "short quote from the stream that grounds this lesson"
}]

Rules:
1. "outcome" MUST be advice to a future agent — "When Y, do X because Z". Achievement reports ("Successfully did X") are banned.
2. steps is the distilled recipe (3-8 steps), NOT a replay of every tool call.
3. rating rubric: 5 = would clearly change how this task class is done (rare); 4 = strong recipe with a non-obvious step; 3 = useful but partly situational (the normal ceiling); below 3 = don't emit it at all.
4. Never invent steps or outcomes not evidenced in the input.
5. Aesthetic-technique items MUST use task_type "art-technique" (the cross-cutting class), never the specific pipeline task (gallery-drop, image-upscale, ...) — one recall of "art-technique" must brief ANY future art session.
6. Output only the JSON array. No preamble, no markdown fences."""


def _session_streams(memories: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """Group jsonl-sync activity batches into per-agent session streams.

    Returns {agent_id: {session_id: [batches, chronological]}}. Only sessions
    with >= STRATEGY_MIN_BATCHES batches survive — slivers carry no trajectory
    worth an LLM call; skips are logged, never silent.
    """
    streams: dict[str, dict[str, list[dict]]] = {}
    for m in memories:
        sid = m.get("session_id") or ""
        if "-jsonl-" not in sid:
            continue
        streams.setdefault(m.get("agent_id", "unknown"), {}).setdefault(sid, []).append(m)

    for agent_id in list(streams):
        skipped = 0
        for sid in list(streams[agent_id]):
            if len(streams[agent_id][sid]) < STRATEGY_MIN_BATCHES:
                del streams[agent_id][sid]
                skipped += 1
            else:
                streams[agent_id][sid].sort(key=lambda x: x.get("timestamp", ""))
        if skipped:
            log.info(
                f"  stage 0.7 [{agent_id}]: skipped {skipped} session(s) with "
                f"< {STRATEGY_MIN_BATCHES} sync batches (too thin to judge)"
            )
        if not streams[agent_id]:
            del streams[agent_id]
    return streams


def _build_session_section(agent_id: str, session_id: str, batches: list[dict],
                           narrative: str) -> str:
    """Render one session's stream (+ narrative context) for the distill call.

    Recency-first cap, same rule as synthesis sections: if the stream exceeds
    STRATEGY_SESSION_MAX_CHARS the oldest batches are dropped, loudly.
    """
    rendered = [(m, _render_memory(m)) for m in batches]
    total = sum(len(r) for _, r in rendered)
    dropped = 0
    if total > STRATEGY_SESSION_MAX_CHARS:
        kept_rev: list[tuple[dict, str]] = []
        budget = STRATEGY_SESSION_MAX_CHARS
        for m, r in reversed(rendered):
            if budget - len(r) < 0:
                break
            kept_rev.append((m, r))
            budget -= len(r)
        dropped = len(rendered) - len(kept_rev)
        rendered = list(reversed(kept_rev))
        log.warning(
            f"  stage 0.7 [{agent_id}] session {session_id}: stream {total:,} chars "
            f"> {STRATEGY_SESSION_MAX_CHARS:,} cap — dropped {dropped} oldest of "
            f"{len(batches)} batches (kept newest {len(rendered)})"
        )
    header = f"# Session stream: agent={agent_id} session={session_id} ({len(rendered)} batches"
    if dropped:
        header += f"; {dropped} older batches omitted"
    header += ")"
    parts = [header] + [r for _, r in rendered]
    if narrative:
        parts.append("\n# NARRATIVE CONTEXT — the agent's own intentional saves from this window")
        parts.append(narrative)
    return "\n".join(parts)


def _narrative_context(memories: list[dict], agent_id: str) -> str:
    """The agent's intentional (non-auto-capture) saves in the window — the
    outcome/lesson signal the raw stream lacks. Newest kept under the cap."""
    intentional = [
        m for m in memories
        if m.get("agent_id") == agent_id and not _is_auto_capture(m)
    ]
    if not intentional:
        return ""
    ordered = sorted(intentional, key=lambda x: x.get("timestamp", ""))
    rendered = [_render_memory(m) for m in ordered]
    text = "\n".join(rendered)
    if len(text) > STRATEGY_NARRATIVE_MAX_CHARS:
        text = text[-STRATEGY_NARRATIVE_MAX_CHARS:]
    return text


def _validate_strategy_items(raw_items: list, agent_id: str, session_id: str) -> list[dict]:
    """Normalize + validate judge output into /trajectory/save bodies.
    Malformed items are dropped with a log line, never a crash."""
    dream_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    valid: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        task_type = str(it.get("task_type") or "").strip()
        task_description = str(it.get("task_description") or "").strip()
        outcome = str(it.get("outcome") or "").strip()
        derived_from = str(it.get("derived_from") or "").strip().lower()
        raw_steps = it.get("steps")
        if not (task_type and task_description and outcome
                and derived_from in ("success", "failure")
                and isinstance(raw_steps, list) and raw_steps):
            log.warning(f"  stage 0.7 [{agent_id}] dropped malformed item: {str(it)[:150]}")
            continue
        steps = []
        for s in raw_steps:
            if isinstance(s, dict) and str(s.get("action") or "").strip():
                steps.append({
                    "action": str(s["action"]),
                    "tool_used": (str(s["tool_used"]) if s.get("tool_used") else None),
                    "result_summary": str(s.get("result_summary") or ""),
                })
            elif isinstance(s, str) and s.strip():
                steps.append({"action": s.strip(), "tool_used": None, "result_summary": ""})
        if not steps:
            log.warning(f"  stage 0.7 [{agent_id}] dropped item with no usable steps: {task_type}")
            continue
        try:
            rating = max(1, min(5, int(it.get("rating", 3))))
        except (TypeError, ValueError):
            rating = 3
        evidence = str(it.get("evidence") or "").strip()
        evidence_source = f"dream:{dream_date} {session_id}"
        if evidence:
            evidence_source += f" — {evidence}"
        valid.append({
            "agent_id": agent_id,
            "task_type": task_type[:128],
            "task_description": task_description[:10000],
            "steps": steps,
            "outcome": outcome[:10000],
            "rating": rating,
            "derived_from": derived_from,
            "source": "dreamer",
            "evidence_source": evidence_source[:500],
            "model": DREAM_MODEL,
        })
    return valid


def distill_strategies_for_agent(agent_id: str, agent_streams: dict[str, list[dict]],
                                 narrative: str) -> list[dict]:
    """Stage 0.7: one distill call per session stream. A failed call or parse
    costs that ONE session, mirroring Stage 0.5's per-chunk isolation."""
    items: list[dict] = []
    for sid in sorted(agent_streams):
        section = _build_session_section(agent_id, sid, agent_streams[sid], narrative)
        try:
            raw, _ = _call_openrouter_adaptive(
                STRATEGY_DISTILL_SYSTEM_PROMPT, section, max_tokens=STRATEGY_MAX_TOKENS
            )
        except RuntimeError as e:
            log.error(f"  stage 0.7 [{agent_id}] session {sid} LLM call failed: {e}")
            continue
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()
        parsed, salvaged = _parse_fact_array(cleaned)
        if salvaged and not parsed:
            log.warning(
                f"  stage 0.7 [{agent_id}] session {sid} JSON parse failed, nothing "
                f"salvageable; head: {cleaned[:200]!r} … tail: {cleaned[-200:]!r}"
            )
            continue
        if salvaged:
            log.warning(
                f"  stage 0.7 [{agent_id}] session {sid} JSON incomplete — salvaged "
                f"{len(parsed)} complete item(s)"
            )
        got = _validate_strategy_items(parsed, agent_id, sid)
        log.info(f"  stage 0.7 [{agent_id}] session {sid}: {len(got)} strategy item(s)")
        items.extend(got)
    return items


def post_strategies(items: list[dict]) -> tuple[int, int]:
    """POST distilled strategies to /trajectory/save. Returns (saved, failed);
    failures are logged per-item and never abort the batch."""
    saved = failed = 0
    for body in items:
        try:
            resp = httpx.post(
                f"{MNEMO_URL}/trajectory/save", json=body,
                headers=MNEMO_AUTH_HEADERS, timeout=30.0,
            )
            if resp.status_code == 200:
                saved += 1
            else:
                failed += 1
                log.warning(
                    f"  /trajectory/save {resp.status_code} for "
                    f"{body['task_type']}: {resp.text[:200]}"
                )
        except httpx.HTTPError as e:
            failed += 1
            log.warning(f"  /trajectory/save exception for {body['task_type']}: {e}")
    return saved, failed


def flag_stale_trajectories() -> list[str]:
    """Curation flag pass (bus #995): trajectories with no save/recall activity
    for STRATEGY_STALE_DAYS. Reads the tenant trajectory JSONLs + recall-stats
    sidecars as plain files — deliberately NOT through TrajectoryStore, so the
    Dreamer never opens the live sqlite-vec index from a second process
    (Windows file-lock trap). Flag-only: review/consolidation is a judgment
    pass that starts once flags exist; nothing is auto-deleted.

    Known limit: only the default AGENTS_ROOT/<agent>/ layout is walked — an
    agent with a custom data_dir override keeps its trajectories elsewhere and
    is not covered by this flag pass."""
    flags: list[str] = []
    now = time.time()
    cutoff = now - STRATEGY_STALE_DAYS * 86400.0
    agents_root = AGENTS_ROOT
    if not agents_root.is_dir():
        return flags
    for agent_dir in sorted(agents_root.iterdir()):
        tdir = agent_dir / "trajectories"
        if not tdir.is_dir():
            continue
        stats: dict = {}
        stats_path = tdir / "recall_stats.json"
        if stats_path.exists():
            try:
                loaded = json.loads(stats_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    stats = loaded
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"  stage 0.7 [{agent_dir.name}] recall_stats.json unreadable: {e}")
        for jsonl in sorted(tdir.glob("*.jsonl")):
            try:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                log.warning(f"  stage 0.7 [{agent_dir.name}] {jsonl.name} unreadable: {e}")
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn line — TrajectoryStore's read path owns that warning
                created = float(rec.get("created_at") or 0)
                rid = rec.get("id") or ""
                last_recalled = float((stats.get(rid) or {}).get("last_recalled") or 0)
                last_activity = max(created, last_recalled)
                if last_activity and last_activity < cutoff:
                    recall_count = int((stats.get(rid) or {}).get("recall_count") or 0)
                    flags.append(
                        f"{agent_dir.name}/{rec.get('task_type', '?')} "
                        f"\"{str(rec.get('task_description') or '')[:80]}\" — "
                        f"inactive {round((now - last_activity) / 86400.0)}d, "
                        f"recalls={recall_count}, rating={rec.get('rating', '?')}, "
                        f"source={rec.get('source', 'agent')}"
                    )
    return flags


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


def _git_sync_repos() -> list[Path]:
    """Repos the git-sync wedge watches. Default: the Dreamer's own checkout
    (catches 'running stale/divergent dreamer code' — the exact drift that bit
    the artforge checkout) plus the brain repo when BRAIN_DIR is set (Opie's
    portable-brain sync spec). MNEMO_DREAM_GIT_SYNC_REPOS (comma-separated paths)
    overrides the defaults."""
    override = os.getenv("MNEMO_DREAM_GIT_SYNC_REPOS", "").strip()
    if override:
        return [Path(p.strip()).expanduser() for p in override.split(",") if p.strip()]
    repos = [Path(__file__).resolve().parent]
    brain = os.getenv("BRAIN_DIR", "").strip()
    if brain:
        repos.append(Path(brain).expanduser())
    return repos


def check_git_sync() -> str | None:
    """Git-sync status of each watched repo. Returns a markdown block, or None
    when disabled. Best-effort: any git failure becomes a ⚠️ line, never raises.
    Reports per repo: dirty working tree, unpushed commits, behind upstream.
    Upstream is resolved via @{u} so it works whether the branch tracks
    origin/main or origin/master."""
    if not MNEMO_DREAM_GIT_SYNC_CHECK:
        return None

    import subprocess

    def _git(repo: Path, *args: str) -> tuple[int, str]:
        try:
            p = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, timeout=30,
            )
            # rstrip (not strip): porcelain status uses a leading-space column
            # on the first line that strip() would eat, shifting the path parse.
            return p.returncode, (p.stdout or p.stderr).rstrip()
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)

    blocks: list[str] = []
    for repo in _git_sync_repos():
        label = repo.name
        if not (repo / ".git").exists():
            continue  # not a git checkout (e.g. BRAIN_DIR is a subdir) — skip silently (audit 2.4)

        lines = [f"**{label}** (`{repo}`)"]

        rc, out = _git(repo, "status", "--porcelain")
        if rc != 0:
            lines.append(f"- ⚠️ could not read working tree: {out[:160]}")
        elif out:
            changed = [ln[3:] for ln in out.splitlines()]
            shown = ", ".join(changed[:8]) + (f" (+{len(changed) - 8} more)" if len(changed) > 8 else "")
            lines.append(f"- ⚠️ {len(changed)} uncommitted change(s): {shown}")
        else:
            lines.append("- ✅ working tree clean")

        rc, upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if rc != 0:
            lines.append("- ⚠️ no upstream tracking branch — skipping ahead/behind")
            blocks.append("\n".join(lines))
            continue

        _git(repo, "fetch", "--quiet")

        rc, out = _git(repo, "log", "--oneline", f"{upstream}..HEAD")
        if rc == 0 and out:
            lines.append(f"- ⚠️ {len(out.splitlines())} unpushed commit(s); newest: {out.splitlines()[0]}")
        elif rc == 0:
            lines.append("- ✅ no unpushed commits")

        rc, out = _git(repo, "log", "--oneline", f"HEAD..{upstream}")
        if rc == 0 and out:
            lines.append(f"- ⚠️ {len(out.splitlines())} behind {upstream} — another machine pushed; pull to refresh")
        elif rc == 0:
            lines.append(f"- ✅ up to date with {upstream}")

        blocks.append("\n".join(lines))

    return "### git sync status\n" + "\n\n".join(blocks)


def notify_git_sync(sync_block: str, dream_date: str) -> None:
    """Push the git-sync block to bus + Discord. Best-effort, same optional/
    log-don't-raise contract as notify_contradictions. Called only when there's
    actionable drift (a ⚠️) so clean nights stay quiet."""
    if MNEMO_DREAM_BUS_URL:
        if MNEMO_DREAM_BUS_FROM and MNEMO_DREAM_BUS_TARGETS:
            for target in MNEMO_DREAM_BUS_TARGETS:
                try:
                    envelope = {
                        "mesh_version": "0.5",
                        "from": MNEMO_DREAM_BUS_FROM,
                        "to": target,
                        "subject": f"dream-git-sync-drift-{dream_date}",
                        "body": {
                            "source": "dreamer",
                            "kind": "git_sync",
                            "dream_date": dream_date,
                            "status": sync_block,
                            "guidance": "A watched git repo has drift. Commit/push on the editing host, or pull on this host, to reconcile before the next session reads stale state.",
                        },
                    }
                    r = httpx.post(f"{MNEMO_DREAM_BUS_URL}/mesh/ping", json=envelope, timeout=10.0)
                    if r.status_code in (200, 201, 202):
                        log.info(f"  git-sync bus notification → {target}: ok")
                    else:
                        log.warning(f"  git-sync bus notification → {target}: HTTP {r.status_code} {r.text[:200]}")
                except httpx.HTTPError as e:
                    log.warning(f"  git-sync bus notification → {target}: {e}")
        else:
            log.warning("  MNEMO_DREAM_BUS_URL set but FROM/TARGETS missing — skipping git-sync bus notification")

    if MNEMO_DREAM_DISCORD_WEBHOOK:
        try:
            content = f"🧠 Brain/repo git-sync drift (dream {dream_date}):\n{sync_block}"
            r = httpx.post(MNEMO_DREAM_DISCORD_WEBHOOK, json={"content": content[:1900]}, timeout=10.0)
            if r.status_code in (200, 204):
                log.info("  git-sync discord webhook posted")
            else:
                log.warning(f"  git-sync discord webhook returned {r.status_code}")
        except httpx.HTTPError as e:
            log.warning(f"  git-sync discord webhook failed: {e}")


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

    # Stage 0.7 — trajectory strategy distillation (v4.7, Trajectory Phase 2).
    # Unlike Stage 0.5 this DOES run on --dry-run: printing the distilled items
    # without posting them is exactly the human quality gate the rollout plan
    # requires (bus #996). Live posting only when the env gate is set AND not
    # a dry run.
    if MNEMO_DREAM_STRATEGIES:
        log.info("Stage 0.7: distilling task strategies from session streams...")
        streams = _session_streams(all_memories)
        if not streams:
            log.info("  stage 0.7: no session activity streams in window")
        total_saved = total_failed = 0
        for agent_id in sorted(streams):
            narrative = _narrative_context(all_memories, agent_id)
            items = distill_strategies_for_agent(agent_id, streams[agent_id], narrative)
            if not items:
                continue
            if args.dry_run:
                print(f"\n--- Stage 0.7 dry-run: {len(items)} strategy item(s) for {agent_id} ---")
                print(json.dumps(items, indent=2, ensure_ascii=False))
            else:
                saved, failed_n = post_strategies(items)
                total_saved += saved
                total_failed += failed_n
        if not args.dry_run and (total_saved or total_failed):
            log.info(f"Stage 0.7: saved {total_saved} strategy item(s), {total_failed} failed")
        stale_flags = flag_stale_trajectories()
        if stale_flags:
            log.info(
                "Stage 0.7 curation flags (no activity in "
                f"{STRATEGY_STALE_DAYS:.0f}d — review, never auto-delete):\n  "
                + "\n  ".join(stale_flags)
            )
    else:
        log.info("Stage 0.7 skipped (MNEMO_DREAM_STRATEGIES not set)")

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

    # Git-sync wedge: surface repo drift (uncommitted / unpushed / behind remote).
    # Always logged + printed for cron.log visibility; pushed to bus + Discord only
    # when there's actionable drift, so clean nights stay quiet.
    sync_block = check_git_sync()
    if sync_block:
        log.info("Git-sync check:\n" + sync_block)
        if "⚠️" in sync_block:
            notify_git_sync(sync_block, dream_date)

    print(f"\n{'='*60}")
    print(f"DREAM COMPLETE — {dream_date}")
    print(f"{'='*60}")
    print(dream_text)
    if sync_block:
        print(f"\n{sync_block}")


if __name__ == "__main__":
    main()
