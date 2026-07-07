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

THE MUSE (v4.8, creative harness) is the Analyst's sibling lens over the same
Tier-2 archive. Same machinery — batching, high-confidence gate, dedup,
deterministic ids, read-once marking (its own muse_processed flag, so both
lenses read every log exactly once, independently) — but the OPPOSITE
temperament: where the Analyst is forbidden to bridge two statements into a
third, bridging two statements into a third is exactly what the Muse is for.
It surfaces the creative material a business note-taker discards — idea seeds,
cross-domain connections, what-ifs, inspirations — as first-class `idea`
memories. One prompt cannot be both ruthless and dreamy (the S111 judge-tuning
lesson); two lenses with two prompts can. The Muse NOTICES, it never INVENTS:
every note must point at material actually voiced in the log.
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


# The Muse may only emit `idea` — it has one job.
MUSE_ALLOWED_CATEGORIES = {"idea"}

MUSE_SYSTEM_PROMPT = """You are the muse-reader for an AI agent's work sessions. You read raw session logs and surface the CREATIVE material a business note-taker would discard: idea seeds, cross-domain connections, what-ifs, inspirations, aesthetic observations.

What qualifies as an idea seed:
- A connection voiced between two domains ("X reminds me of Y", "X is like Y", "X could work the way Y does")
- A what-if or "wouldn't it be" possibility someone raised and did not pursue
- An inspiration or aesthetic observation (visual, musical, spatial, narrative) stated in the log
- A reframing that visibly opened up the conversation, even mid-riff

Rules:
1. NOTICE, never INVENT. The idea must be present in the log — voiced by the user or the agent. You name it and make it self-contained; you never add connections of your own.
2. The riff is the signal. Speculative language ("what if", "imagine", "might be cool", "reminds me of") marks candidates, not noise — the opposite of how a fact extractor reads it.
3. Observations ABOUT the work are NEVER ideas: bugs found, dependency gaps, config tensions, lessons learned, process insights, "this highlights the importance of X". Those are the fact-taker's material. If a note could be filed as a decision, incident, or doctrine, it is not an idea seed — do not emit it. This includes ECHOES: restating one of the project's own existing principles, rules, or mottos — however aptly, and however it is dressed ("a valuable heuristic", "a useful filter") — is applying a known principle, not creating. If the log names a principle the team already lives by, it is not an idea seed.
4. The test: an idea seed points OUTWARD, toward something new that could be made, tried, or explored beyond the current task. A note that points INWARD, at how the work itself went, fails the test.
5. Skip: task work, decisions already made, status chatter, tool output, and small talk with no idea inside. A plan that was executed is a task, not an idea.
6. Each note must be SELF-CONTAINED and name BOTH sides of any connection — a reader with zero session context must be able to pick the thread back up.
7. "summary": ONE sentence naming the idea and where it points next. "key_facts": 2-5 searchable anchors (the domains bridged, the metaphor, the names involved).
8. "confidence": "high" only when the idea is unmistakably present in the log; otherwise "low". Low-confidence notes are discarded.
9. An empty list is valid AND COMMON. Most work sessions contain no idea seeds — a log full of technical work with no voiced creative connection yields [] and that is the CORRECT reading of it, not a failure. More than 3 notes from one batch is almost always over-extraction.
10. ONE note per underlying idea. Before output, compare your candidates: if two trace back to the same moment, pattern, or connection in the logs, keep only the strongest — a rewording is not a second idea. Two notes may share a domain, but never a source thread.

Output ONLY a JSON array, no preamble:
[{"category": "idea", "summary": "...", "key_facts": ["..."], "confidence": "high"}]"""


def _strip_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


def _parse_notes(raw: str, max_notes: int, allowed: set[str] = ALLOWED_CATEGORIES) -> list[dict]:
    """Validate the LLM reply down to well-formed, high-confidence notes."""
    try:
        # strict=False: raw LLM output may carry literal newlines inside JSON
        # strings (the S111.5 lesson — any parser fed LLM output needs this).
        data = json.loads(_strip_fences(raw), strict=False)
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
        if category not in allowed or not summary or len(summary) > 1000:
            continue
        key_facts = [str(f).strip()[:300] for f in (item.get("key_facts") or [])
                     if str(f).strip()][:5]
        notes.append({"category": category, "summary": summary, "key_facts": key_facts})
        if len(notes) >= max_notes:
            break
    return notes


def _gather_candidates(
    memory_dir: Path, limit: int, marker: str = "analyst_processed"
) -> list[tuple[Path, dict]]:
    """Oldest-first unprocessed session_log memories (each is read once, ever).

    `marker` scopes the read-once bookkeeping per lens: the Analyst and the
    Muse each read every log exactly once, independently."""
    candidates = []
    for path in memory_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except Exception:
            continue
        if entry.get("category") != "session_log":
            continue
        if entry.get(marker):
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
    """One Analyst pass over a tenant (conservative fact lens). Returns stats:
    {scanned, batches, notes_extracted, notes_deduped, notes_saved, failed}."""
    return await _lens_pass(
        agent_id, memory_dir, vec_store, reasoner, embedder,
        config=config, lens="analyst", system_prompt=ANALYST_SYSTEM_PROMPT,
        allowed=ALLOWED_CATEGORIES, marker="analyst_processed",
    )


async def muse_tenant(
    agent_id: str,
    memory_dir: Path,
    vec_store,
    reasoner,
    embedder,
    *,
    config,
    dry_run: bool = False,
) -> dict:
    """One Muse pass over a tenant (creative idea lens). Emits `idea` memories.

    dry_run=True is the Guy's-Gate instrument: gather → LLM → parse → return
    the notes in stats["notes"] WITHOUT embedding, dedup, persisting, or
    marking sources processed. No vec/embedder access at all, so it is safe to
    run from a second process against a live store (the S111 sqlite lock trap).
    """
    return await _lens_pass(
        agent_id, memory_dir, vec_store, reasoner, embedder,
        config=config, lens="muse", system_prompt=MUSE_SYSTEM_PROMPT,
        allowed=MUSE_ALLOWED_CATEGORIES, marker="muse_processed",
        dry_run=dry_run,
    )


async def _lens_pass(
    agent_id: str,
    memory_dir: Path,
    vec_store,
    reasoner,
    embedder,
    *,
    config,
    lens: str,
    system_prompt: str,
    allowed: set[str],
    marker: str,
    dry_run: bool = False,
) -> dict:
    """One extraction pass over a tenant's unprocessed session logs, through
    one lens (analyst = conservative facts, muse = idea seeds). Returns stats:
    {scanned, batches, notes_extracted, notes_deduped, notes_saved, failed}
    (+ "notes" when dry_run).

    All LLM/embedding calls run with use_breaker=False — this is background
    batch work and must not touch the live breakers (batch-vs-live isolation).
    """
    label = lens.capitalize()
    stats: dict = {"scanned": 0, "batches": 0, "notes_extracted": 0,
                   "notes_deduped": 0, "notes_saved": 0, "failed": 0}
    candidates = _gather_candidates(memory_dir, config.max_memories_per_cycle,
                                    marker=marker)
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
            "\n\n".join(lines), system=system_prompt,
            max_tokens=1500, use_breaker=False,
        )
        notes = _parse_notes(raw, config.max_notes_per_batch, allowed=allowed)
    except Exception as e:
        log.warning(f"{label} LLM pass failed for '{agent_id}': {e}")
        stats["failed"] = len(batch)
        return stats  # sources NOT marked processed — retried next cycle

    stats["notes_extracted"] = len(notes)

    if dry_run:
        # Guy's-Gate instrument: show what the lens WOULD save. No embedding,
        # no dedup gate (that needs the live vec index), no persistence, and
        # sources stay unmarked so the real pass reads them again.
        stats["notes"] = notes
        log.info(
            f"{label} DRY RUN '{agent_id}': read {stats['scanned']} logs → "
            f"{len(notes)} note(s) extracted (nothing saved, nothing marked)"
        )
        return stats

    now = time.time()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for note in notes:
        # Defense in depth: sources were redacted at ingest, but the note text
        # is LLM output — run it through the same choke point anyway.
        summary, _ = redact_text(note["summary"])
        key_facts = [redact_text(f)[0] for f in note["key_facts"]]
        full_text = summary + ("\n" + "\n".join(key_facts) if key_facts else "")

        try:
            embedding = await embedder.embed(full_text, use_breaker=False, task_type="document")
        except Exception as e:
            log.warning(f"{label} embed failed for '{agent_id}': {e}")
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
            log.warning(f"{label} dedup check failed (saving anyway): {e}")

        # Deterministic id: re-running over the same sources + text can't
        # duplicate a note.
        memory_id = hashlib.sha256(
            f"{lens}:{agent_id}:{summary}".encode()
        ).hexdigest()[:16]
        entry = {
            "id": memory_id,
            "session_id": f"{lens}-{agent_id}-{date_str}",
            "agent_id": agent_id,
            "summary": summary,
            "key_facts": key_facts,
            "projects_referenced": [],
            "decisions_made": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "created_at": now,
            "source": "inferred",
            "category": note["category"],
            "additional_tags": [lens],
            "classified_by": lens,
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
                category=note["category"],  # #468: without this, every analyst/muse note lands NULL in the search pre-filter column
            )
            stats["notes_saved"] += 1
            emoji = "🎨" if lens == "muse" else "📝"
            log.info(f"{emoji} {label} note [{note['category']}] for '{agent_id}': {summary[:100]}")
        except Exception as e:
            log.error(f"{label} persist failed for '{agent_id}': {e}")
            stats["failed"] += 1

    # Any per-note failure (embed/persist) leaves the WHOLE batch unmarked so
    # it retries next cycle — otherwise the failed note's insight is lost
    # forever (source read-once, note never derived). Retry is idempotent:
    # deterministic memory_ids + the dedup gate absorb the notes that DID save.
    if stats["failed"]:
        log.warning(
            f"{label} '{agent_id}': {stats['failed']} note(s) failed to persist — "
            f"batch left unmarked for retry next cycle"
        )
        return stats

    # Mark sources processed — including when zero notes came back. "Nothing
    # worth keeping" is an answer; re-reading the same logs nightly is not.
    for path, entry in batch:
        try:
            # Re-read before writing: `entry` was loaded before seconds of LLM
            # latency, and writing that stale copy back would clobber anything
            # a concurrent writer changed in between (a reclassify pass, the
            # other lens's marker). Patch only the field this lens owns.
            # A failed re-read skips the mark rather than writing the stale
            # copy (which would also resurrect a file deleted mid-flight);
            # the unmarked log simply retries next cycle — the dedup gate
            # absorbs any notes that already saved.
            try:
                fresh = json.loads(path.read_text())
            except Exception as e:
                log.warning(f"Re-read before {marker} mark failed for {path} "
                            f"— left unmarked for retry: {e}")
                continue
            fresh[marker] = True
            path.write_text(json.dumps(fresh, indent=2, default=str))
        except Exception as e:
            log.warning(f"Failed to mark {path} {marker}: {e}")

    return stats
