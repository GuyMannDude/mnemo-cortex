"""
Mnemo Cortex — the Analyst: smart session analysis (v4.1, roadmap Phase 2)
==========================================================================
The original vision was an assistant that takes notes at a meeting — someone
who notices the decisions, traps, and stated preferences without being told
to write them down. Until now that someone didn't exist: manual saves caught
what an agent remembered to save, auto-capture caught everything else as
undifferentiated Tier-2 logs, and nothing in between read those logs and
asked "what here is actually worth keeping?"

The Analyst is that layer. On a maintenance cadence it walks each tenant's
unprocessed session_log memories (the Tier-2 archive), asks the reasoning LLM
to extract the few notes a future session genuinely needs, dedups them against
what the store already knows (true cosine against existing vectors), and
persists the survivors as first-class Tier-1 memories with provenance:
source="inferred", classified_by="analyst", derived_from=[source ids].

Conservatism is the design center, encoded three ways:
  1. The prompt demands stated facts only, says an empty list is the COMMON
     correct answer, and requires self-contained notes.
  2. Only confidence="high" notes survive parsing.
  3. The dedup gate drops anything the store already knows (>= 0.90 cosine).
A noisy note-taker would just recreate the firehose this system spent v4.0
digging out of.

Every source log is marked analyst_processed (even when nothing was worth
extracting) so each is read exactly once. Tier 2 stays intact — the Analyst
distills, it never deletes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from agentb.cache import cosine_similarity
from agentb.redact import redact_text

log = logging.getLogger("agentb.analyst")

# Categories the Analyst may emit. session_log/unknown are deliberately
# absent — the whole point is to climb OUT of those buckets.
ALLOWED_CATEGORIES = {
    "decision", "incident", "doctrine", "identity",
    "relationship", "topology", "current_state",
}

ANALYST_SYSTEM_PROMPT = """You are the silent note-taker for an AI agent's work sessions. You read raw session logs and extract ONLY the few things a future session genuinely needs:

- decision: a choice made or ruled out, WITH its reason
- incident: something that broke — the trap and the fix
- doctrine: a rule, preference, or principle the user stated or reinforced
- identity: who a person/agent/system is (name, role)
- relationship: a customer, partner, or collaborator fact
- topology: a host/port/service/path fact stated as enduring truth
- current_state: a project-status fact that matters beyond today

Rules:
1. CONSERVATIVE EXTRACTION ONLY. Extract what is stated directly. Do NOT infer, do NOT bridge two statements into a third. If in doubt, skip.
2. Skip routine activity: file reads, command output, status chatter, plans completed within the same session.
3. Each note must be SELF-CONTAINED — a reader with zero session context must understand it.
4. "summary": ONE dense sentence, leading with the why. "key_facts": 2-5 concrete searchable anchors (paths, ports, versions, names, error strings).
5. "confidence": "high" only when the log states it plainly; otherwise "low". Low-confidence notes are discarded.
6. An empty list is valid AND COMMON. Most session logs contain nothing worth keeping. That is the correct answer, not a failure.

Output ONLY a JSON array, no preamble:
[{"category": "...", "summary": "...", "key_facts": ["..."], "confidence": "high"}]"""


def _strip_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


def _parse_notes(raw: str, max_notes: int) -> list[dict]:
    """Validate the LLM reply down to well-formed, high-confidence notes."""
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        log.warning(f"Analyst JSON parse failed: {e}; head: {raw[:120]!r}")
        return []
    if not isinstance(data, list):
        return []
    notes = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("confidence", "")).lower() != "high":
            continue
        category = str(item.get("category", "")).strip()
        summary = str(item.get("summary", "")).strip()
        if category not in ALLOWED_CATEGORIES or not summary or len(summary) > 1000:
            continue
        key_facts = [str(f).strip()[:300] for f in (item.get("key_facts") or [])
                     if str(f).strip()][:5]
        notes.append({"category": category, "summary": summary, "key_facts": key_facts})
        if len(notes) >= max_notes:
            break
    return notes


def _gather_candidates(memory_dir: Path, limit: int) -> list[tuple[Path, dict]]:
    """Oldest-first unprocessed session_log memories (each is read once, ever)."""
    candidates = []
    for path in memory_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except Exception:
            continue
        if entry.get("category") != "session_log":
            continue
        if entry.get("analyst_processed"):
            continue
        candidates.append((path, entry))
    candidates.sort(key=lambda pe: pe[1].get("created_at") or 0)
    return candidates[:limit]


async def analyze_tenant(
    agent_id: str,
    memory_dir: Path,
    vec_store,
    reasoner,
    embedder,
    *,
    config,
) -> dict:
    """One analysis pass over a tenant. Returns stats:
    {scanned, batches, notes_extracted, notes_deduped, notes_saved, failed}.

    All LLM/embedding calls run with use_breaker=False — this is background
    batch work and must not touch the live breakers (batch-vs-live isolation).
    """
    stats = {"scanned": 0, "batches": 0, "notes_extracted": 0,
             "notes_deduped": 0, "notes_saved": 0, "failed": 0}
    candidates = _gather_candidates(memory_dir, config.max_memories_per_cycle)
    if not candidates:
        return stats

    # Pack candidates into one batch up to max_batch_chars; the rest waits for
    # the next cycle. Per-memory truncation keeps one giant log from eating
    # the whole batch.
    batch: list[tuple[Path, dict]] = []
    lines: list[str] = []
    used = 0
    for path, entry in candidates:
        text = (entry.get("summary") or "")[: config.per_memory_chars]
        facts = entry.get("key_facts") or []
        if facts:
            text += "\n" + "\n".join(f"- {f}" for f in facts[:6])[:400]
        block = f"[log {entry.get('id', path.stem)} @ {entry.get('timestamp', '?')[:16]}]\n{text}"
        if used + len(block) > config.max_batch_chars and batch:
            break
        batch.append((path, entry))
        lines.append(block)
        used += len(block)

    stats["scanned"] = len(batch)
    stats["batches"] = 1
    source_ids = [e.get("id", p.stem) for p, e in batch]

    try:
        raw = await reasoner.generate(
            "\n\n".join(lines), system=ANALYST_SYSTEM_PROMPT,
            max_tokens=1500, use_breaker=False,
        )
        notes = _parse_notes(raw, config.max_notes_per_batch)
    except Exception as e:
        log.warning(f"Analyst LLM pass failed for '{agent_id}': {e}")
        stats["failed"] = len(batch)
        return stats  # sources NOT marked processed — retried next cycle

    stats["notes_extracted"] = len(notes)
    now = time.time()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for note in notes:
        # Defense in depth: sources were redacted at ingest, but the note text
        # is LLM output — run it through the same choke point anyway.
        summary, _ = redact_text(note["summary"])
        key_facts = [redact_text(f)[0] for f in note["key_facts"]]
        full_text = summary + ("\n" + "\n".join(key_facts) if key_facts else "")

        try:
            embedding = await embedder.embed(full_text, use_breaker=False)
        except Exception as e:
            log.warning(f"Analyst embed failed for '{agent_id}': {e}")
            stats["failed"] += 1
            continue

        # Dedup gate: if the store already knows this (>= threshold cosine
        # against the nearest existing memory), don't save it again.
        try:
            nearest = vec_store.search(embedding, top_k=1)
            if nearest:
                known = vec_store.get_embedding(nearest[0].memory_id)
                if known and cosine_similarity(embedding, known) >= config.dedup_similarity:
                    stats["notes_deduped"] += 1
                    continue
        except Exception as e:
            log.warning(f"Analyst dedup check failed (saving anyway): {e}")

        # Deterministic id: re-running over the same sources + text can't
        # duplicate a note.
        memory_id = hashlib.sha256(
            f"analyst:{agent_id}:{summary}".encode()
        ).hexdigest()[:16]
        entry = {
            "id": memory_id,
            "session_id": f"analyst-{agent_id}-{date_str}",
            "agent_id": agent_id,
            "summary": summary,
            "key_facts": key_facts,
            "projects_referenced": [],
            "decisions_made": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "created_at": now,
            "source": "inferred",
            "category": note["category"],
            "additional_tags": ["analyst"],
            "classified_by": "analyst",
            "derived_from": source_ids,
            "schema_version": 3,
        }
        try:
            (memory_dir / f"{memory_id}.json").write_text(
                json.dumps(entry, indent=2, default=str))
            vec_store.upsert(
                memory_id, full_text, embedding,
                source_file=(memory_dir / f"{memory_id}.json").as_posix(),
                created_at=now,
            )
            stats["notes_saved"] += 1
            log.info(f"📝 Analyst note [{note['category']}] for '{agent_id}': {summary[:100]}")
        except Exception as e:
            log.error(f"Analyst persist failed for '{agent_id}': {e}")
            stats["failed"] += 1

    # Mark sources processed — including when zero notes came back. "Nothing
    # worth keeping" is an answer; re-reading the same logs nightly is not.
    for path, entry in batch:
        try:
            entry["analyst_processed"] = True
            path.write_text(json.dumps(entry, indent=2, default=str))
        except Exception as e:
            log.warning(f"Failed to mark {path} analyst_processed: {e}")

    return stats
