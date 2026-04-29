#!/usr/bin/env python3
"""Mnemo Wiki Compiler — generates topic-axis wiki pages from Mnemo data.

Doctrine: Mnemo is the source of truth. The wiki is a compiled view that is
*always regenerable* from Mnemo. Wiki pages are NEVER edited directly. If the
wiki is wrong, fix the source memories in Mnemo and recompile.

Runs as a nightly cron at 3:30 AM on THE VAULT, 15 minutes after Dreaming
finishes. Same data sources as Dreaming (AgentB writebacks + Mnemo v2 SQLite),
same LLM (gemini-2.5-flash via OpenRouter), but produces topic-axis pages
instead of a time-axis daily brief.

Per-page failures do NOT kill the run — bad LLM call → ⚠️ to #alerts → continue.

Usage:
  mnemo-wiki-compile.py                     # nightly default (since last compile)
  mnemo-wiki-compile.py --dry-run --verbose # show clustering, NO LLM calls
  mnemo-wiki-compile.py --days 7            # explicit time window
  mnemo-wiki-compile.py --topics rocky-gallery,sparks-bus  # subset compile
  mnemo-wiki-compile.py --full              # all-time recompile (expensive!)

Author: CC + Guy — Project Sparks (2026-04-22)
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config — mirrors mnemo-dream.py for consistency
# ---------------------------------------------------------------------------

AGENTB_DATA_DIR = Path(os.getenv("AGENTB_DATA_DIR", "~/.agentb")).expanduser()
MNEMO_DB_PATH = Path(os.getenv("MNEMO_DB_PATH", "~/.mnemo-v2/mnemo.sqlite3")).expanduser()
WIKI_DIR = Path(os.getenv("WIKI_DIR", "~/wiki")).expanduser()

# Compiler writes only to these sections. `sources/` is owned by the
# file-inventory job, not us.
COMPILER_SECTIONS = ("projects", "entities", "concepts")

# Per-run state — locked file, single writer.
COMPILE_STATE_PATH = WIKI_DIR / ".compile-state.json"
COMPILE_LOG_DIR = AGENTB_DATA_DIR / "wiki-compile-log"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
WIKI_MODEL = os.getenv("MNEMO_WIKI_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Discord (reuses Sparks Bus config — same channels file, same token)
DISCORD_TOKEN_FILE = Path(os.getenv("DISCORD_TOKEN_FILE", str(Path.home() / ".sparks/discord-token")))
CHANNELS_FILE = Path(os.getenv(
    "CHANNELS_FILE",
    str(Path.home() / "mnemo-plan/discord-channels.json"),
))
ALERTS_CHANNEL = os.getenv("ALERTS_CHANNEL", "alerts")

# Bloat soft-cap; flag pages over this for human review.
MAX_PAGE_WORDS = 6000

# Audit thresholds.
STALE_DAYS = 30
THIN_COVERAGE = 3

# Skip auto-capture noise; we don't want tool-flush rows compiling pages.
SKIP_KEY_FACTS = {"auto_capture_flush"}

# Agent → entity-page aliases. Captures the writing agent as an implicit topic
# of every writeback (a CC memory contributes to entities/cc; a BW memory to
# entities/bullwinkle). Also seeds nickname matching in summary text.
AGENT_ALIASES: dict[str, list[str]] = {
    "cc": ["cc", "claude code", "claude-code"],
    "opie": ["opie"],
    "rocky": ["rocky"],
    "bw": ["bw", "bullwinkle"],
    "bullwinkle": ["bw", "bullwinkle"],
    "cliff": ["cliff"],
    "sparky": ["sparky"],
    "alice": ["alice"],
    "guy": ["guy"],
}

log = logging.getLogger("mnemo-wiki")


# ---------------------------------------------------------------------------
# Harvest — same shape as dream.py so the compiler sees what dreams see
# ---------------------------------------------------------------------------

def harvest_agentb(since: datetime) -> list[dict]:
    """Read all AgentB writeback JSONs newer than `since`."""
    memories = []
    memory_root = AGENTB_DATA_DIR / "memory"
    if not memory_root.exists():
        log.warning(f"AgentB memory dir not found: {memory_root}")
        return memories

    for agent_dir in memory_root.iterdir():
        if not agent_dir.is_dir() or agent_dir.name == "dreamer":
            continue
        agent_id = agent_dir.name
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


def harvest_mnemo_sqlite(since: datetime) -> list[dict]:
    """Read summaries and substantive assistant messages from Mnemo v2."""
    if not MNEMO_DB_PATH.exists():
        log.warning(f"Mnemo DB not found at {MNEMO_DB_PATH}")
        return []

    conn = sqlite3.connect(str(MNEMO_DB_PATH))
    conn.row_factory = sqlite3.Row
    memories = []
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

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
        })

    for row in conn.execute("""
        SELECT c.agent_id, c.session_id, m.content, m.role, m.created_at
        FROM messages m
        JOIN conversations c ON c.conversation_id = m.conversation_id
        WHERE m.created_at > ? AND m.role = 'assistant'
        ORDER BY m.created_at DESC
        LIMIT 100
    """, (since_str,)):
        content = row["content"]
        if len(content) > 100:
            memories.append({
                "source": "mnemo-v2-message",
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "timestamp": row["created_at"],
                "summary": content[:2000],
                "key_facts": [],
                "projects": [],
                "decisions": [],
            })
    conn.close()
    return memories


# ---------------------------------------------------------------------------
# Slug + topic clustering — Python deterministic, NOT LLM routing
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name.lower()).strip()
    s = re.sub(r"[-\s]+", "-", s)
    return s.strip("-")


def existing_pages_by_section() -> dict[str, set[str]]:
    """Snapshot {section: {slug, slug, ...}} of pages currently on disk."""
    out: dict[str, set[str]] = {s: set() for s in COMPILER_SECTIONS}
    for section in COMPILER_SECTIONS:
        d = WIKI_DIR / section
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            out[section].add(f.stem)
    return out


def cluster_memories(memories: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Deterministic cluster: memory -> (section, slug) keys.

    A single memory may land in multiple buckets (e.g., touches both a project
    and an entity). The LLM later sees the full memory in each cluster, but
    only synthesizes once per (section, slug).

    Routing rules:
      - 'projects' field present  → projects/{slug} for each project
      - matches an existing entities/{slug} page (case-insensitive substring
        in summary or key_facts)  → entities/{slug}
      - matches an existing concepts/{slug} page (same)  → concepts/{slug}

    Memories that match nothing are dropped — not everything earns a page.
    """
    pages = existing_pages_by_section()
    entity_slugs = pages["entities"]
    concept_slugs = pages["concepts"]

    clusters: dict[tuple[str, str], list[dict]] = {}

    def add(key: tuple[str, str], m: dict):
        clusters.setdefault(key, []).append(m)

    # Build alias lookup: alias text -> entity slug (only for entities that have a page).
    alias_to_slug: dict[str, str] = {}
    for canonical, aliases in AGENT_ALIASES.items():
        if canonical in entity_slugs:
            for a in aliases:
                alias_to_slug[a.lower()] = canonical
        else:
            # Map aliases to whichever known entity slug they point to.
            for a in aliases:
                if a.lower() in entity_slugs:
                    for other in aliases:
                        alias_to_slug[other.lower()] = a.lower()
                    break

    for m in memories:
        if m.get("key_facts") and all(f in SKIP_KEY_FACTS for f in m["key_facts"]):
            continue

        agent_id = (m.get("agent_id") or "").lower()
        # Include agent_id and its aliases in the haystack so the writing agent
        # is implicitly a topic of every memory it produced.
        agent_terms = [agent_id] + AGENT_ALIASES.get(agent_id, [])

        haystack = " ".join([
            (m.get("summary") or "")[:4000],
            *(f for f in m.get("key_facts", []) if f not in SKIP_KEY_FACTS),
            *m.get("decisions", []),
            *agent_terms,
        ]).lower()

        landed_anywhere = False

        for project in m.get("projects", []):
            if project:
                add(("projects", slugify(project)), m)
                landed_anywhere = True

        for slug in entity_slugs:
            patterns = [slug, slug.replace("-", " ")]
            # Pull in nicknames if this slug has aliases registered.
            for alias_text, target in alias_to_slug.items():
                if target == slug:
                    patterns.append(alias_text)
            for pat in patterns:
                if pat and re.search(rf"\b{re.escape(pat)}\b", haystack):
                    add(("entities", slug), m)
                    landed_anywhere = True
                    break

        for slug in concept_slugs:
            for pat in (slug, slug.replace("-", " ")):
                if pat and re.search(rf"\b{re.escape(pat)}\b", haystack):
                    add(("concepts", slug), m)
                    landed_anywhere = True
                    break

        if not landed_anywhere:
            log.debug(f"unrouted memory: {m.get('session_id')} agent={m.get('agent_id')}")

    return clusters


# ---------------------------------------------------------------------------
# Compile state — manual-edit detection + flock
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not COMPILE_STATE_PATH.exists():
        return {"hashes": {}, "last_compiled": {}, "last_run": None}
    try:
        return json.loads(COMPILE_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"hashes": {}, "last_compiled": {}, "last_run": None}


def save_state(state: dict) -> None:
    COMPILE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COMPILE_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(COMPILE_STATE_PATH)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def detect_manual_edit(state: dict, page_path: Path) -> bool:
    """If the on-disk hash differs from our last-recorded write hash → manual edit."""
    rel = str(page_path.relative_to(WIKI_DIR))
    stored = state["hashes"].get(rel)
    if not stored or not page_path.exists():
        return False
    return file_sha(page_path) != stored


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------

WIKI_SYSTEM_PROMPT = """You are the Mnemo Wiki Compiler for Project Sparks. Your job: take a topic and a cluster of recent memories about it, plus any existing wiki page, and produce a fully-rewritten wiki page that integrates everything.

DOCTRINE (do not violate):
- The wiki is a COMPILED view. Mnemo is the source of truth. Your output replaces the existing page; preserve substance from the existing page that's still accurate, drop substance contradicted by new memories, integrate new substance.
- Be specific: file paths, version numbers, dates, names. No generic prose.
- Vapor Truth: state what is known. If there's contradiction in the memories, surface it explicitly under a "Contradictions" subsection — do NOT smooth it away. Conflicts are signal.
- Cross-references: if you mention another wiki topic by name (a person, a project, a concept), mention it cleanly so the compiler can wikilink it. Do NOT invent wikilinks; the compiler validates them.
- Keep the page tight. Aim for under 2000 words. If the topic genuinely needs more, fine — the compiler will flag pages over 6000 words.

OUTPUT FORMAT — produce exactly these sections, in this order, in markdown:

## Summary
A 2-4 paragraph synthesis of what this topic IS, what's true about it right now, and what changed recently.

## Key Facts
Bulleted list of concrete facts (paths, versions, names, dates, decisions).

## Timeline
Chronological list of significant events. Use date prefixes (YYYY-MM-DD).

## Cross References
Bulleted list of related topic names (just the names, not wikilinks). The compiler will validate and wikilink them.

## Contradictions
Only include this section IF the memories conflict with each other or with the existing page. Each item: what conflicts, with which sources, why it matters. Omit the section entirely if there are no conflicts.

Do NOT include the page title (the compiler adds it). Do NOT include front-matter (the compiler adds it). Do NOT include the source-memories footer (the compiler adds it). Just the five sections above."""


def call_llm(prompt: str) -> tuple[str, dict]:
    """Returns (text, usage_dict). Raises on non-200."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    r = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://projectsparks.ai",
            "X-Title": "Mnemo Wiki Compiler",
        },
        json={
            "model": WIKI_MODEL,
            "messages": [
                {"role": "system", "content": WIKI_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 4096,
            "temperature": 0.3,
        },
        timeout=120.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:400]}")
    j = r.json()
    return j["choices"][0]["message"]["content"], j.get("usage", {})


def build_user_prompt(section: str, slug: str, memories: list[dict], existing: str | None) -> str:
    lines = [
        f"# Topic: {section}/{slug}",
        "",
        f"You're compiling the wiki page for `{section}/{slug}`.",
        "",
    ]
    if existing:
        lines += [
            "## Existing page (rewrite to integrate new info; drop what's contradicted; preserve what's still true)",
            "",
            existing.strip(),
            "",
            "---",
            "",
        ]
    else:
        lines += ["(No existing page — this is a fresh compile.)", ""]

    lines += [f"## New memories ({len(memories)} entries)", ""]
    for m in sorted(memories, key=lambda x: x.get("timestamp", "")):
        ts = (m.get("timestamp") or "?")[:19]
        lines.append(f"### [{ts}] {m.get('agent_id', '?')} session={m.get('session_id', '?')}")
        lines.append(m.get("summary", "").strip()[:2400])
        if m.get("key_facts"):
            facts = [f for f in m["key_facts"] if f not in SKIP_KEY_FACTS]
            if facts:
                lines.append("Key facts:")
                lines += [f"  - {f}" for f in facts]
        if m.get("decisions"):
            lines.append("Decisions: " + "; ".join(m["decisions"]))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cross-reference validation — never hallucinate a wikilink
# ---------------------------------------------------------------------------

def validate_cross_refs(body: str, all_pages: dict[str, set[str]]) -> tuple[str, list[str]]:
    """Walk the 'Cross References' section: for each line that names a topic,
    rewrite to [[section/slug]] if a real page exists. Drop names without a backing page.

    Returns (rewritten_body, validated_link_list).
    """
    lines = body.split("\n")
    out: list[str] = []
    in_xref = False
    valid_links: list[str] = []

    flat_index: dict[str, str] = {}
    for section, slugs in all_pages.items():
        for slug in slugs:
            flat_index[slug] = f"{section}/{slug}"
            flat_index[slug.replace("-", " ")] = f"{section}/{slug}"

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## cross references") or \
           stripped.lower().startswith("## cross-references"):
            in_xref = True
            out.append(line)
            continue
        if in_xref and stripped.startswith("##"):
            in_xref = False  # next section
            out.append(line)
            continue

        if in_xref and stripped.startswith(("-", "*")):
            text = stripped.lstrip("-*").strip()
            text = re.sub(r"^\[\[(.*?)\]\]$", r"\1", text).strip()
            text_lc = text.lower()
            match = None
            for needle, target in flat_index.items():
                if needle and (needle == text_lc or needle in text_lc):
                    match = target
                    break
            if match:
                out.append(f"- [[{match}]]")
                valid_links.append(match)
            # else: drop the line silently — no hallucinated wikilinks
            continue

        out.append(line)

    return "\n".join(out), valid_links


# ---------------------------------------------------------------------------
# Page render + write
# ---------------------------------------------------------------------------

def render_page(section: str, slug: str, body: str, memories: list[dict]) -> str:
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    word_count = len(body.split())
    needs_split = word_count > MAX_PAGE_WORDS

    # Source memories footer — every claim above is auditable through these.
    src_lines = ["## Source Memories", ""]
    src_lines.append(f"This page was compiled from {len(memories)} Mnemo entries:")
    src_lines.append("")
    for m in sorted(memories, key=lambda x: x.get("timestamp", "")):
        sid = m.get("session_id", "unknown")
        ts = (m.get("timestamp") or "?")[:19]
        agent = m.get("agent_id", "?")
        src_lines.append(f"- `{sid}` — {ts} — agent={agent}")

    title = slug.replace("-", " ").title()
    front_matter = "\n".join([
        "---",
        "compiled-by: mnemo-wiki-compiler",
        f"section: {section}",
        f"slug: {slug}",
        f"last-compiled: {now_iso}",
        f"source-memory-count: {len(memories)}",
        f"word-count: {word_count}",
        f"needs-split: {'true' if needs_split else 'false'}",
        "---",
        "",
    ])

    header = "\n".join([
        f"# {title}",
        "",
        f"**Last compiled:** {now_iso}  ",
        f"**Section:** `{section}`  ",
        f"**Source memories:** {len(memories)}",
        "",
        "---",
        "",
    ])

    footer = "\n".join([
        "",
        "---",
        "",
        *src_lines,
        "",
        "---",
        "",
        "*Auto-generated from Mnemo Cortex. Source of truth is Mnemo. Manual edits will be overwritten on the next compile. To correct this page, fix the source memories in Mnemo and recompile.*",
        "",
    ])

    return front_matter + header + body.strip() + footer


def write_page(section: str, slug: str, content: str, state: dict) -> Path:
    page_path = WIKI_DIR / section / f"{slug}.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    if page_path.exists():
        prev = page_path.with_suffix(".md.prev")
        prev.write_bytes(page_path.read_bytes())  # one-deep rollback
    page_path.write_text(content)
    rel = str(page_path.relative_to(WIKI_DIR))
    state["hashes"][rel] = file_sha(page_path)
    state["last_compiled"][rel] = datetime.now(timezone.utc).isoformat()
    return page_path


# ---------------------------------------------------------------------------
# Discord alert (reuses Sparks Bus token + channels file)
# ---------------------------------------------------------------------------

def post_alert(content: str) -> bool:
    try:
        token = DISCORD_TOKEN_FILE.read_text().strip()
        channels = json.loads(CHANNELS_FILE.read_text()).get("channels", {})
        channel_id = channels.get(ALERTS_CHANNEL)
        if not channel_id:
            log.error(f"Alerts channel {ALERTS_CHANNEL!r} not in {CHANNELS_FILE}")
            return False
        r = httpx.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"content": content[:1900]},
            timeout=15.0,
        )
        if not r.is_success:
            log.error(f"Discord alert failed: {r.status_code} {r.text[:200]}")
        return r.is_success
    except Exception as e:
        log.error(f"Discord alert error: {e}")
        return False


# ---------------------------------------------------------------------------
# Index regeneration
# ---------------------------------------------------------------------------

def regenerate_index() -> Path:
    """Rebuild WIKI_DIR/INDEX.md from the on-disk page set. Preserves the
    existing markdown-with-wikilinks format. Source files are owned by another
    pipeline; we list them best-effort but never edit them."""
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "# WikAI Index",
        "",
        f"*Generated: {now}*",
        "",
    ]
    total = 0
    for section in COMPILER_SECTIONS:
        d = WIKI_DIR / section
        if not d.is_dir():
            continue
        pages = sorted(d.glob("*.md"))
        if not pages:
            continue
        lines.append(f"## {section.title()}")
        lines.append("")
        for p in pages:
            slug = p.stem
            tagline = first_section_summary(p)
            lines.append(f"- [[{section}/{slug}]] — {tagline}" if tagline else f"- [[{section}/{slug}]]")
            total += 1
        lines.append("")

    # Preserve existing sources/ section as-is by referencing whatever's there.
    src_dir = WIKI_DIR / "sources"
    if src_dir.is_dir():
        src_pages = sorted(src_dir.glob("*.md"))
        if src_pages:
            lines.append("## Sources")
            lines.append("")
            lines.append(f"*{len(src_pages)} pages — managed by file-inventory pipeline, not the compiler.*")
            lines.append("")

    lines.insert(3, f"*Total compiler-owned pages: {total}*")
    lines.insert(4, "")

    index_path = WIKI_DIR / "INDEX.md"
    index_path.write_text("\n".join(lines))
    return index_path


def first_section_summary(page: Path) -> str:
    """Pull the first non-empty paragraph from the Summary section (best-effort)."""
    try:
        text = page.read_text()
    except OSError:
        return ""
    m = re.search(r"^## Summary\s*\n(.*?)(?=\n##|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    para = m.group(1).strip().split("\n\n", 1)[0].strip()
    if not para or para.startswith("#"):
        return ""
    return (para[:140] + "…") if len(para) > 140 else para


# ---------------------------------------------------------------------------
# Audit summary — surfaces drift, runs every nightly (no separate flag)
# ---------------------------------------------------------------------------

def audit(state: dict, just_compiled: set[str]) -> dict:
    now = datetime.now(timezone.utc)
    stale: list[str] = []
    thin: list[tuple[str, int]] = []
    manually_edited: list[str] = []

    for section in COMPILER_SECTIONS:
        d = WIKI_DIR / section
        if not d.is_dir():
            continue
        for page in d.glob("*.md"):
            rel = str(page.relative_to(WIKI_DIR))
            if rel in just_compiled:
                continue
            last = state["last_compiled"].get(rel)
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if now - last_dt > timedelta(days=STALE_DAYS):
                        stale.append(rel)
                except ValueError:
                    pass
            try:
                head = page.read_text()
                m = re.search(r"^source-memory-count:\s*(\d+)", head, re.MULTILINE)
                if m and int(m.group(1)) < THIN_COVERAGE:
                    thin.append((rel, int(m.group(1))))
            except OSError:
                pass
            if detect_manual_edit(state, page):
                manually_edited.append(rel)
    return {"stale": stale, "thin": thin, "manually_edited": manually_edited}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_last_run(state: dict) -> datetime | None:
    last = state.get("last_run")
    if not last:
        return None
    try:
        return datetime.fromisoformat(last)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Mnemo Wiki Compiler")
    parser.add_argument("--dry-run", action="store_true", help="Cluster + plan, no LLM calls or writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--days", type=int, help="Time window in days (default: since last compile)")
    parser.add_argument("--topics", help="Comma-separated section/slug pairs to compile (e.g., projects/sparks-bus,entities/guy)")
    parser.add_argument("--full", action="store_true", help="All-time recompile — expensive, run sparingly")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [wiki-compile] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )

    if not WIKI_DIR.is_dir():
        log.error(f"WIKI_DIR not found: {WIKI_DIR}")
        return 1

    # Single-writer lock — defense against accidental concurrent compiles.
    COMPILE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = COMPILE_STATE_PATH.with_suffix(".lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error(f"Another compiler holds the lock at {lock_path}")
        return 1

    try:
        state = load_state()

        # Time window
        if args.full:
            since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elif args.days:
            since = datetime.now(timezone.utc) - timedelta(days=args.days)
        else:
            last_run = get_last_run(state)
            since = last_run if last_run else datetime.now(timezone.utc) - timedelta(days=7)
        log.info(f"Harvest window: since {since.isoformat()}")

        # Harvest
        memories = harvest_agentb(since) + harvest_mnemo_sqlite(since)
        log.info(f"Harvested {len(memories)} memories")
        if not memories:
            log.info("No new memories — nothing to compile")
            return 0

        # Cluster
        clusters = cluster_memories(memories)
        log.info(f"Clustered into {len(clusters)} topics")

        # Optional subset filter
        if args.topics:
            wanted = set()
            for t in args.topics.split(","):
                if "/" in t:
                    section, slug = t.strip().split("/", 1)
                    wanted.add((section, slug))
                else:
                    log.warning(f"--topics entry needs section/slug: {t!r}")
            clusters = {k: v for k, v in clusters.items() if k in wanted}
            log.info(f"Filtered to {len(clusters)} topics by --topics")

        if args.dry_run:
            log.info("DRY RUN — would compile:")
            for (section, slug), mems in sorted(clusters.items()):
                log.info(f"  {section}/{slug}  ({len(mems)} memories)")
            return 0

        # Compile each cluster — one LLM call per topic, isolated failures
        all_pages_snapshot = existing_pages_by_section()
        compiled_rels: set[str] = set()
        failures: list[tuple[str, str]] = []
        total_usage = {"prompt": 0, "completion": 0}

        for (section, slug), mems in sorted(clusters.items()):
            if section not in COMPILER_SECTIONS:
                log.debug(f"skip non-compiler section: {section}/{slug}")
                continue
            try:
                page_path = WIKI_DIR / section / f"{slug}.md"

                # Manual-edit warning (does NOT block; we overwrite per doctrine)
                if detect_manual_edit(state, page_path):
                    log.warning(f"manual edit detected on {section}/{slug} — will be overwritten")

                existing = page_path.read_text() if page_path.exists() else None
                prompt = build_user_prompt(section, slug, mems, existing)
                log.info(f"compiling {section}/{slug}  ({len(mems)} memories, {len(prompt):,} chars)")

                body, usage = call_llm(prompt)
                total_usage["prompt"] += usage.get("prompt_tokens", 0)
                total_usage["completion"] += usage.get("completion_tokens", 0)

                # Refresh page snapshot for cross-ref validation (we may have just made one)
                all_pages_snapshot.setdefault(section, set()).add(slug)
                body, links = validate_cross_refs(body, all_pages_snapshot)

                full = render_page(section, slug, body, mems)
                write_page(section, slug, full, state)
                compiled_rels.add(f"{section}/{slug}.md")
                log.info(f"  -> wrote {section}/{slug}.md  ({len(links)} cross-refs)")
            except Exception as e:
                msg = f"{section}/{slug}: {e}"
                log.error(f"COMPILE FAILED  {msg}")
                failures.append((f"{section}/{slug}", str(e)))
                post_alert(
                    f"⚠️ [Wiki] COMPILE FAILED: {section}/{slug}\n"
                    f"Memories: {len(mems)}\n"
                    f"Error: {str(e)[:400]}\n"
                    f"Status: page not updated; other pages still compiled"
                )

        # Index regen
        index_path = regenerate_index()
        log.info(f"index regenerated: {index_path}")

        # Audit
        audit_report = audit(state, compiled_rels)
        log.info(
            f"audit: stale>{STALE_DAYS}d={len(audit_report['stale'])} "
            f"thin<{THIN_COVERAGE}={len(audit_report['thin'])} "
            f"manually_edited={len(audit_report['manually_edited'])}"
        )
        if args.verbose:
            for rel in audit_report["stale"][:20]:
                log.info(f"  stale: {rel}")
            for rel, n in audit_report["thin"][:20]:
                log.info(f"  thin: {rel} ({n} memories)")
            for rel in audit_report["manually_edited"][:20]:
                log.info(f"  manually-edited: {rel}")

        # Persist run state
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        # Log summary
        COMPILE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = COMPILE_LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"
        log_path.write_text(json.dumps({
            "run_at": state["last_run"],
            "since": since.isoformat(),
            "harvested": len(memories),
            "clustered_topics": len(clusters),
            "compiled": sorted(compiled_rels),
            "failures": failures,
            "audit": {
                "stale_count": len(audit_report["stale"]),
                "thin_count": len(audit_report["thin"]),
                "manually_edited_count": len(audit_report["manually_edited"]),
                "stale": audit_report["stale"],
                "thin": [{"page": p, "memory_count": n} for p, n in audit_report["thin"]],
                "manually_edited": audit_report["manually_edited"],
            },
            "usage": total_usage,
        }, indent=2))
        log.info(f"compile log: {log_path}")
        log.info(f"tokens used: {total_usage['prompt']} prompt + {total_usage['completion']} completion")

        return 0 if not failures else 2
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
