"""
Mnemo Cortex — Smart Ingestion classifier (v4.0)
================================================
LLM-powered category classification with a cheap noise pre-filter.

Two-tier memory model (bus #600/#601):
  - TIER 1 "Smart Notes": the eight meaningful categories below. Clean,
    categorized, recalled first (recall excludes session_log by default).
  - TIER 2 "Session Logs": raw auto-captured conversation/tool-call logs.
    Tagged `session_log`, kept as the archive, excluded from default recall.

The defect this fixes: the regex auto-suggester (`provenance.suggest_category`)
silently returns "unknown" whenever it can't keyword-match, so real memories AND
raw logs both land in one bucket and compete for the same top-k slots. Here we:
  1. demote routine logs to `session_log` for FREE (no LLM) via `is_routine_log`,
  2. classify everything else with the reasoning LLM into a real Tier-1 category,
  3. fall back to the regex suggester only when the LLM is unavailable.

This module is the single source of truth, reused by the /writeback hook
(server), the nightly dreamer pass, and the `migrate reclassify` CLI.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from agentb.fsutil import atomic_write_text
from agentb.provenance import suggest_category

log = logging.getLogger("agentb.classify")

# The eight Tier-1 categories the LLM may choose from. Deliberately EXCLUDES
# "unknown" (the whole point is to stop defaulting there) and is the target set
# for genuine memories. `session_log` is reachable only via the noise heuristic.
CLASSIFIABLE_CATEGORIES: list[tuple[str, str]] = [
    ("topology", "infrastructure: hosts, ports, IPs, services, processes, file paths, system layout"),
    ("current_state", "live project status — what is in flight or true right now"),
    ("doctrine", "a preference, rule, principle, convention, or policy to follow"),
    ("incident", "a bug, crash, outage, regression, postmortem, or failure"),
    ("identity", "who an agent/person/persona is — name, role, self-description"),
    ("relationship", "a customer, partner, vendor, collaborator, or person we work with"),
    ("decision", "a choice made or ruled out, with rationale"),
    ("idea", "a creative insight, cross-domain connection, inspiration, aesthetic observation, or what-if — an idea seed, not yet a decision or task"),
    ("session_log", "raw conversation or tool-call log with no distilled fact"),
]

_VALID_TARGETS = {c for c, _ in CLASSIFIABLE_CATEGORIES}

_SYSTEM_PROMPT = (
    "You classify a single memory into exactly ONE category for a memory system. "
    "Choose the best fit from this list:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in CLASSIFIABLE_CATEGORIES)
    + "\n\nReply with ONLY the category word, nothing else. Do not explain."
)

DEFAULT_MAX_INPUT_CHARS = 1500

# Routine-log signatures (the Tier-2 firehose). Cheap, no LLM.
_AUTOSYNC_RE = re.compile(r"\(auto-sync from JSONL", re.IGNORECASE)
_TOOLONLY_RE = re.compile(r"^(CC|Opie|Rocky|Dave)?\s*invoked tool[: ]", re.IGNORECASE)


def is_routine_log(summary: str, key_facts: list[str] | None) -> bool:
    """True when this entry is a raw session/tool log, not a distilled memory.

    These belong in Tier 2 (`session_log`) and must never cost an LLM call.
    """
    s = (summary or "").strip()
    facts = key_facts or []
    if not s:
        return True
    if s.startswith("[AUTO-CAPTURE]"):
        return True
    if _AUTOSYNC_RE.search(s):
        return True
    # auto_capture_flush sentinel, or a key_facts list that is only tool-call noise
    if facts and all(
        (f or "").strip().lower() == "auto_capture_flush" or _TOOLONLY_RE.search((f or "").strip())
        for f in facts
    ):
        return True
    if _TOOLONLY_RE.search(s):
        return True
    return False


def _parse_category(raw: str) -> str | None:
    """Extract a valid Tier-1 category from a (possibly chatty) LLM reply.

    Exact one-word reply wins. Otherwise accept a single unambiguous category
    mention; if the reply names two different categories (e.g. "not topology,
    it's a decision") treat it as ambiguous and return None so the caller falls
    back to the regex suggester rather than guessing.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if text in _VALID_TARGETS:
        return text
    found: list[str] = []
    tokens = re.findall(r"[a-z_]+", text)
    for token in tokens:
        if token in _VALID_TARGETS and token not in found:
            found.append(token)
    if len(found) != 1:
        return None
    if found[0] == "idea" and len(tokens) > 1:
        # "idea" is the one category name that is ordinary chat vocabulary
        # ("i have no idea", "one idea would be..."). A mention inside a
        # chatty reply is noise, not an answer — accept it only as the sole
        # token ("idea", "**idea**"); otherwise fall back to the regex
        # suggester like any unparseable reply.
        return None
    return found[0]


async def classify_category(
    reasoner,
    summary: str,
    key_facts: list[str] | None,
    *,
    use_breaker: bool = True,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
) -> tuple[str, str]:
    """Classify a memory into a category.

    Returns (category, method) where method is one of:
      "noise-heuristic" — demoted to session_log without an LLM call
      "llm"             — categorized by the reasoning model
      "regex"           — LLM unavailable/invalid; fell back to the regex suggester

    Never raises: a failed LLM call degrades to the regex suggester so a save is
    never blocked on the classifier.
    """
    if is_routine_log(summary, key_facts):
        return "session_log", "noise-heuristic"

    text = (summary or "")
    if key_facts:
        text = text + "\n" + "\n".join(key_facts)
    text = text.strip()[:max_input_chars]

    if reasoner is not None and text:
        try:
            raw = await reasoner.generate(
                text, system=_SYSTEM_PROMPT, max_tokens=8, use_breaker=use_breaker
            )
            cat = _parse_category(raw)
            if cat:
                return cat, "llm"
            log.warning(f"LLM classification returned no valid category: {raw!r:.80}")
        except Exception as e:
            log.warning(f"LLM classification failed, falling back to regex: {e}")

    # Fallback: regex suggester (may itself return "unknown" — caller flags for retry)
    suggested = suggest_category(text)[0]
    return suggested, "regex"


async def reclassify_memory_dir(
    memory_dir,
    reasoner,
    *,
    limit: int | None = None,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    include_routine: bool = True,
    dry_run: bool = False,
    use_breaker: bool = False,
    on_progress=None,
    on_reclassified=None,
) -> dict:
    """Walk a tenant's memory dir and reclassify the entries that need it.

    A candidate is any memory whose category is `unknown`/missing, that carries
    `needs_reclassification`, or (when include_routine) is a routine log not yet
    tagged `session_log`. Only the JSON `category` (+ `classified_by`) field is
    rewritten — embeddings are never recomputed. As of #468, category is also a
    `vec_sources` pre-filter column; pass `on_reclassified(memory_id, new_cat)`
    so the caller can refresh that column (otherwise category-filtered recall
    would see a stale value). Shared by the nightly dreamer pass (limit-capped)
    and the `migrate reclassify` CLI (full sweep with backup).

    `on_progress(done, total)` is called as the scan advances. Returns stats:
    {scanned, reclassified, skipped, failed, by_category, by_method}.
    """
    memory_dir = Path(memory_dir)
    stats = {
        "scanned": 0, "reclassified": 0, "skipped": 0, "failed": 0,
        "by_category": {}, "by_method": {},
    }
    files = sorted(memory_dir.glob("*.json"))
    total = len(files)
    for i, path in enumerate(files):
        if on_progress:
            on_progress(i, total)
        if limit is not None and stats["reclassified"] >= limit:
            break
        try:
            entry = json.loads(path.read_text())
        except Exception as e:
            stats["failed"] += 1
            log.warning(f"Skipping unreadable memory {path}: {e}")
            continue
        stats["scanned"] += 1
        cat = entry.get("category")
        summary = entry.get("summary", "") or ""
        key_facts = entry.get("key_facts") or []
        candidate = (
            cat in (None, "unknown")
            or entry.get("needs_reclassification")
            or (include_routine and cat != "session_log" and is_routine_log(summary, key_facts))
        )
        if not candidate:
            stats["skipped"] += 1
            continue

        new_cat, method = await classify_category(
            reasoner, summary, key_facts,
            use_breaker=use_breaker, max_input_chars=max_input_chars,
        )
        stats["by_category"][new_cat] = stats["by_category"].get(new_cat, 0) + 1
        stats["by_method"][method] = stats["by_method"].get(method, 0) + 1
        stats["reclassified"] += 1
        if dry_run:
            continue

        # Re-read before writing: seconds of LLM latency sit between the read
        # at the top of this loop and this write — writing the stale copy back
        # would clobber anything a concurrent writer changed in between (the
        # Analyst/Muse processed markers, a manual edit). Patch only the
        # fields this pass owns.
        try:
            entry = json.loads(path.read_text())
        except Exception as e:
            stats["failed"] += 1
            log.warning(f"Re-read before reclassify write failed for {path}: {e}")
            continue
        entry["category"] = new_cat
        entry["classified_by"] = method
        if method == "regex":
            entry["needs_reclassification"] = True
        else:
            entry.pop("needs_reclassification", None)
        try:
            # Atomic: rewrites an existing memory in place — see analyst.py;
            # same torn-read / crash-truncation window since v4.9.14.
            atomic_write_text(path, json.dumps(entry, indent=2, default=str))
        except Exception as e:
            stats["failed"] += 1
            log.error(f"Failed to write reclassified memory {path}: {e}")
            continue
        # #468: the JSON category is now mirrored in vec_sources.category as a
        # search pre-filter. Notify the caller so it can refresh the column —
        # a stale column would wrongly exclude this memory from category recall.
        # Only fire after the disk write succeeds (the column tracks disk truth).
        if on_reclassified is not None:
            memory_id = entry.get("id") or path.stem
            try:
                on_reclassified(memory_id, new_cat)
            except Exception as e:
                log.warning(f"on_reclassified hook failed for {memory_id}: {e}")
    if on_progress:
        on_progress(total, total)
    return stats
