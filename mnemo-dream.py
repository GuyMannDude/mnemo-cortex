#!/usr/bin/env python3
"""
Mnemo Dreaming — Nightly Cross-Agent Memory Synthesis
=====================================================
Reads the day's memories from ALL agents, synthesizes them into a single
brief, and writes that brief back into AgentB so every agent gets it at
startup.

Runs as a nightly cron job on THE VAULT.

Data sources:
  - AgentB writebacks: ~/.agentb/memory/{agent}/*.json  (CC, Rocky, Opie, etc.)
  - Mnemo v2 SQLite:   ~/.mnemo-v2/mnemo.sqlite3        (Alice — messages + summaries)

Output:
  - Writes dream brief to ~/.agentb/memory/dreamer/{dream-id}.json
  - Also writes human-readable markdown to ~/.agentb/dreams/YYYY-MM-DD.md

Usage:
  python3 mnemo-dream.py                  # Normal nightly run
  python3 mnemo-dream.py --dry-run        # Show what would be synthesized, don't write
  python3 mnemo-dream.py --hours 48       # Override time window (default: since last dream)
  python3 mnemo-dream.py --verbose        # Show all harvested memories before synthesis

Author: CC + Guy — Project Sparks
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

AGENTB_DATA_DIR = Path(os.getenv("AGENTB_DATA_DIR", "/home/guy/.agentb"))
MNEMO_DB_PATH = Path(os.getenv("MNEMO_DB_PATH", "/home/guy/.mnemo-v2/mnemo.sqlite3"))
DREAM_DIR = AGENTB_DATA_DIR / "dreams"
DREAMER_MEMORY_DIR = AGENTB_DATA_DIR / "memory" / "dreamer"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
DREAM_MODEL = os.getenv("MNEMO_DREAM_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Agents to harvest from AgentB writebacks
AGENTB_AGENTS = ["cc", "rocky", "opie", "bw", "cliff", "sparky"]

# Skip auto-capture noise (rocky's tool call flushes)
SKIP_AUTO_CAPTURE = False  # Keep everything — Guy said details matter

log = logging.getLogger("mnemo-dream")

# ---------------------------------------------------------------------------
# Harvest: AgentB writebacks (CC, Rocky, Opie, etc.)
# ---------------------------------------------------------------------------

def harvest_agentb(since: datetime) -> list[dict]:
    """Read all AgentB writeback JSONs newer than `since`."""
    memories = []
    memory_root = AGENTB_DATA_DIR / "memory"

    for agent_dir in memory_root.iterdir():
        if not agent_dir.is_dir():
            continue
        agent_id = agent_dir.name
        if agent_id == "dreamer":
            continue  # Don't eat our own dreams

        for f in agent_dir.glob("*.json"):
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
# Harvest: Mnemo v2 SQLite (Alice)
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

DREAM_SYSTEM_PROMPT = """You are the memory synthesizer for a multi-agent workspace called Project Sparks.

The workspace has these agents:
- CC (Claude Code): Primary builder. Handles code, infrastructure, deployments, system configuration.
- Rocky: OpenClaw agent on IGOR. Handles conversations with Guy, research, store management.
- Opie: Claude Desktop agent. Handles extended sessions, research, UI work, store setup.
- Alice: OpenClaw agent on THE VAULT. Creative/technical specialist.
- BW (Bullwinkle): Agent Zero in Docker. Misc tasks.
- Cliff: Occasional helper.

Guy is the human operator — 73-year-old maker in Half Moon Bay, CA. Project Sparks makes 3D printed seasonal collectibles.

Your job: read the last period's memories from ALL agents and produce a synthesis brief that answers:

1. **What was built or shipped** — specific deliverables, with file paths and versions where available
2. **What was decided** — choices made, directions set, approaches validated or rejected
3. **What's blocked or pending** — open issues, next steps, things waiting on Guy or external systems
4. **Cross-agent connections** — things one agent did that another should know about
5. **Lessons learned** — failures, workarounds, doctrines reinforced

Be specific. Names, paths, versions, error messages. No fluff, no filler. Every sentence should carry information.

Format as markdown with the sections above. Keep it dense but readable. This brief will be injected into each agent's startup context tomorrow morning."""

def synthesize(memories: list[dict], dry_run: bool = False) -> str:
    """Send harvested memories to the LLM for synthesis."""

    # Group by agent for clarity
    by_agent: dict[str, list[dict]] = {}
    for m in memories:
        agent = m["agent_id"]
        if agent not in by_agent:
            by_agent[agent] = []
        by_agent[agent].append(m)

    # Build the prompt
    sections = []
    total_chars = 0
    for agent_id, agent_memories in sorted(by_agent.items()):
        lines = [f"## Agent: {agent_id} ({len(agent_memories)} entries)"]
        for m in sorted(agent_memories, key=lambda x: x.get("timestamp", "")):
            ts = m.get("timestamp", "?")[:19]
            lines.append(f"\n### [{ts}] session={m.get('session_id', '?')}")
            lines.append(m["summary"])
            if m["key_facts"]:
                lines.append("Key facts:")
                for fact in m["key_facts"]:
                    if fact != "auto_capture_flush":  # Skip noise markers
                        lines.append(f"  - {fact}")
            if m.get("decisions"):
                lines.append("Decisions: " + "; ".join(m["decisions"]))
        section = "\n".join(lines)
        total_chars += len(section)
        sections.append(section)

    user_content = "# Agent Memories to Synthesize\n\n" + "\n\n---\n\n".join(sections)

    log.info(f"Synthesis input: {len(memories)} memories from {len(by_agent)} agents, {total_chars:,} chars")

    if dry_run:
        return f"[DRY RUN] Would synthesize {len(memories)} memories from {len(by_agent)} agents ({total_chars:,} chars)"

    if not OPENROUTER_API_KEY:
        log.error("No OPENROUTER_API_KEY set — cannot call LLM")
        sys.exit(1)

    # Call OpenRouter
    response = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://projectsparks.ai",
            "X-Title": "Mnemo Dreaming",
        },
        json={
            "model": DREAM_MODEL,
            "messages": [
                {"role": "system", "content": DREAM_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 4096,
            "temperature": 0.3,
        },
        timeout=120.0,
    )

    if response.status_code != 200:
        log.error(f"OpenRouter returned {response.status_code}: {response.text[:500]}")
        sys.exit(1)

    result = response.json()
    dream_text = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    log.info(f"LLM usage: {usage.get('prompt_tokens', '?')} prompt, {usage.get('completion_tokens', '?')} completion")

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

    # Also POST through /writeback so the dream hits L2 index + Mem0
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
            },
            timeout=15.0,
        )
        if wb_response.status_code == 200:
            wb_data = wb_response.json()
            log.info(f"Dream synced to bridge (L2 + Mem0): memory_id={wb_data.get('memory_id', '?')}")
        else:
            log.warning(f"Bridge writeback returned {wb_response.status_code}")
    except Exception as e:
        log.warning(f"Bridge writeback failed (non-fatal): {e}")

    return dream_id


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

    log.info("Harvesting Mnemo v2 SQLite (Alice)...")
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

    # Synthesize
    log.info(f"Sending to {DREAM_MODEL} for synthesis...")
    dream_text = synthesize(all_memories, dry_run=args.dry_run)

    if args.dry_run:
        print(dream_text)
        return

    # Write
    dream_id = write_dream(dream_text, all_memories, since)
    log.info(f"Dream complete: id={dream_id}")
    print(f"\n{'='*60}")
    print(f"DREAM COMPLETE — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print(f"{'='*60}")
    print(dream_text)


if __name__ == "__main__":
    main()
