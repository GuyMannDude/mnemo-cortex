"""Mnemo Cortex Trajectory Learning — Phase 1 (v4.5.0).

A native learning loop: agents capture the step-by-step recipe of a task that
went well (`mnemo_save_trajectory`) and recall proven recipes before a similar
task (`mnemo_recall_trajectory`). No external deps, no fine-tuning, no automatic
capture — agents explicitly save when they judge a task succeeded.

Storage mirrors the rest of Mnemo's "JSONL is disk truth, sqlite-vec is the
index" philosophy:

  - {agent_data_dir}/trajectories/{task_type}.jsonl
        Append-only, crash-safe source of truth. One JSON object per line, each
        with a UUID + timestamp. A torn final line (crash mid-write) is skipped
        on read, never corrupting earlier entries.
  - {agent_data_dir}/trajectories/traj_index.sqlite
        A VecStore (the same per-tenant sqlite-vec index used for memories) over
        each trajectory's embedding text, keyed by trajectory id. The embedding
        text is task_description + outcome + step actions; the task_type rides
        the index `category` column so recall can filter by task_type inside the
        kNN. Recall joins the vec hits back to the full JSONL records (which
        carry the step sequence, rating, and recency) and ranks them.

Phase-1 boundaries (Opie spec 2026-06-25): no export, no fine-tuning, no
automatic capture, no cross-agent sharing — each agent's trajectories are its
own under its agent data dir.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from agentb.vec import VecStore, MAX_EMBED_INPUT_CHARS

log = logging.getLogger("agentb.trajectory")

# Ranking weights for recall. The spec ranks results by (1) semantic similarity,
# (2) rating, (3) recency — descending priority. A weighted composite with
# descending weights honors that order while still letting a strong rating or a
# fresh recipe break a near-tie on similarity. Similarity dominates so an
# off-topic-but-5-star recipe can never outrank an on-topic one.
SIM_WEIGHT = 0.60
RATING_WEIGHT = 0.25
RECENCY_WEIGHT = 0.15

# Recency half-life: a 30-day-old trajectory keeps half its recency credit.
RECENCY_HALFLIFE_DAYS = 30.0

# Overfetch from the vec index before rating-filtering + composite re-ranking,
# so a min_rating filter that drops the nearest few still has candidates left.
RECALL_OVERFETCH = 6


def sanitize_task_type(task_type: str) -> str:
    """Map a task_type to a safe JSONL filename stem.

    task_type becomes a filename, so it must not enable path traversal or odd
    filesystem characters. Lowercase, keep [a-z0-9_-], collapse everything else
    to underscore. The ORIGINAL (unsanitized) task_type is still stored inside
    each record and used as the vec index category, so recall filtering stays
    exact — only the filename is normalized.
    """
    stem = re.sub(r"[^a-z0-9_-]+", "_", (task_type or "").strip().lower()).strip("_")
    return stem or "untagged"


def _similarity(distance: float) -> float:
    """vec0 distance (L2) → 0..1 similarity, matching the /context handler."""
    return 1.0 / (1.0 + distance)


def _recency_factor(created_at: Optional[float], now: float) -> float:
    """Exponential decay on age. Fresh = ~1.0, one half-life old = 0.5."""
    if not created_at:
        return 0.0
    age_days = max(0.0, (now - created_at) / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def embedding_text(task_description: str, outcome: str, steps: list[dict]) -> str:
    """Build the text that gets embedded for similarity recall.

    task_description is the primary signal (recall queries describe what's about
    to be done); outcome and the step actions add 'what was actually done' so a
    query phrased around the mechanics still matches. Capped at the embedder's
    safe input size — an oversize input would 400 and trip the breaker.
    """
    parts = [task_description or "", outcome or ""]
    parts.extend(s.get("action", "") for s in (steps or []) if s.get("action"))
    text = "\n".join(p for p in parts if p).strip()
    if len(text) > MAX_EMBED_INPUT_CHARS:
        text = text[:MAX_EMBED_INPUT_CHARS]
    return text


class TrajectoryStore:
    """Per-tenant trajectory recipes: append-only JSONL + a sqlite-vec index."""

    def __init__(self, traj_dir: Path):
        self.traj_dir = traj_dir
        self.traj_dir.mkdir(parents=True, exist_ok=True)
        self.vec = VecStore(traj_dir / "traj_index.sqlite")
        # task_type stem -> (mtime, {id: record}). Avoids re-parsing JSONL on
        # every recall; invalidated when the file's mtime changes.
        self._cache: dict[str, tuple[float, dict[str, dict]]] = {}
        # Recall reinforcement (v4.7): mutable counters live in a sidecar JSON,
        # NOT in the recipe JSONLs — those are append-only crash-safe truth, and
        # in-place mutation would break their torn-line recovery story. The
        # sidecar is rewritten whole via tmp+rename (atomic on POSIX and NTFS).
        self._stats_path = traj_dir / "recall_stats.json"

    def close(self) -> None:
        self.vec.close()

    def _jsonl_path(self, task_type: str) -> Path:
        return self.traj_dir / f"{sanitize_task_type(task_type)}.jsonl"

    # ── Write ──

    def save(
        self,
        *,
        agent_id: Optional[str],
        task_type: str,
        task_description: str,
        steps: list[dict],
        outcome: str,
        rating: int,
        embedding: list[float],
        token_cost: Optional[int] = None,
        model: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        derived_from: Optional[str] = None,
        source: str = "agent",
        evidence_source: Optional[str] = None,
    ) -> dict:
        """Append a trajectory to its JSONL and index its embedding.

        JSONL append happens FIRST and is the durable record; the vec upsert is
        the index. If the upsert raises, the trajectory is on disk but is NOT
        recallable — recall is gated by the vec index, and Phase 1 ships no
        reindex/backfill path for trajectories. The caller (endpoint) surfaces
        that failure rather than returning a false success (Vapor Truth). A
        reindex that re-embeds orphaned JSONL rows is a Phase-2 follow-up.
        Returns the stored record.
        """
        traj_id = uuid.uuid4().hex
        ts = time.time()
        record = {
            "id": traj_id,
            "agent_id": agent_id,
            "task_type": task_type,
            "task_description": task_description,
            "steps": steps,
            "outcome": outcome,
            "rating": rating,
            "token_cost": token_cost,
            "model": model,
            "duration_seconds": duration_seconds,
            # v4.7 provenance: source="agent" (hand-saved recipe, Phase 1) or
            # "dreamer" (Stage 0.7 distilled strategy). derived_from marks
            # whether a distilled lesson came from a success or a failure —
            # hand-saved recipes are implicitly successes and leave it None.
            "derived_from": derived_from,
            "source": source,
            "evidence_source": evidence_source,
            "timestamp": _iso(ts),
            "created_at": ts,
        }

        path = self._jsonl_path(task_type)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        # Invalidate this file's parsed cache. The cache keys on st_mtime, but a
        # second save landing in the same coarse-mtime tick would leave the
        # cached record set stale — recall would then silently drop the new
        # trajectory even though the vec index returns its id. Drop the entry so
        # the next recall re-reads disk truth.
        self._cache.pop(sanitize_task_type(task_type), None)

        # category = the ORIGINAL task_type, so include_category filtering in
        # recall is exact (the filename is sanitized; the category is not).
        self.vec.upsert(
            traj_id,
            embedding_text(task_description, outcome, steps),
            embedding,
            source_file=path.as_posix(),
            created_at=ts,
            category=task_type,
        )
        log.info(
            f"Trajectory saved: {traj_id} (agent={agent_id or 'default'}, "
            f"task_type={task_type!r}, rating={rating})"
        )
        return record

    # ── Read ──

    def _load(self, task_type: Optional[str]) -> dict[str, dict]:
        """Return {id: record} for one task_type, or all task_types if None.

        Per-file mtime cache. Malformed lines (including a torn final line from
        a crash mid-append) are skipped with a warning so one bad line never
        hides the rest of the file.
        """
        if task_type is not None:
            return self._load_file(self._jsonl_path(task_type))
        merged: dict[str, dict] = {}
        for path in sorted(self.traj_dir.glob("*.jsonl")):
            merged.update(self._load_file(path))
        return merged

    def _load_file(self, path: Path) -> dict[str, dict]:
        if not path.exists():
            return {}
        mtime = path.stat().st_mtime
        cached = self._cache.get(path.stem)
        if cached and cached[0] == mtime:
            return cached[1]
        records: dict[str, dict] = {}
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning(f"Skipping malformed trajectory line in {path}: {e}")
                    continue
                rid = rec.get("id")
                if rid:
                    records[rid] = rec
        self._cache[path.stem] = (mtime, records)
        return records

    # ── Recall stats (v4.7 reinforcement) ──

    def _load_stats(self) -> dict[str, dict]:
        if not self._stats_path.exists():
            return {}
        try:
            data = json.loads(self._stats_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            # Stats are reinforcement metadata, not recipe truth — a corrupt
            # sidecar must never break recall. Start fresh, loudly.
            log.warning(f"recall_stats.json unreadable ({e}); resetting stats")
            return {}

    def _bump_recall_stats(self, traj_ids: list[str]) -> None:
        # Structural guarantee: reinforcement metadata must NEVER sink a recall.
        # Shape-coerce each entry (a sidecar that parses as JSON can still hold
        # scalars or junk counts), and catch everything else at the boundary —
        # the caller has already built its results.
        if not traj_ids:
            return
        try:
            stats = self._load_stats()
            now = time.time()
            for tid in traj_ids:
                entry = stats.get(tid)
                if not isinstance(entry, dict):
                    entry = {}
                    stats[tid] = entry
                try:
                    count = int(entry.get("recall_count") or 0)
                except (TypeError, ValueError):
                    count = 0
                entry["recall_count"] = count + 1
                entry["last_recalled"] = now
            tmp = self._stats_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._stats_path)
        except Exception as e:
            log.warning(f"recall stats bump failed (non-fatal, recall unaffected): {e}")

    def recall(
        self,
        query_embedding: list[float],
        *,
        task_type: Optional[str] = None,
        min_rating: int = 3,
        max_results: int = 3,
    ) -> list[dict]:
        """Return up to max_results full trajectory records ranked by the
        spec's (similarity, rating, recency) priority, filtered to rating >=
        min_rating.

        Each returned record is the stored JSONL object plus a `_score` block
        ({similarity, composite}) so callers can show why it ranked.
        """
        records = self._load(task_type)
        if not records:
            return []
        hits = self.vec.search(
            query_embedding,
            top_k=max(max_results * RECALL_OVERFETCH, max_results),
            include_category=task_type,
        )
        now = time.time()
        scored: list[tuple[float, float, dict]] = []
        for h in hits:
            rec = records.get(h.memory_id)
            if rec is None:
                # In the index but not loadable from JSONL — e.g. a malformed or
                # torn line was skipped on read. Disk is authority; skip it.
                continue
            if int(rec.get("rating", 0)) < min_rating:
                continue
            sim = _similarity(h.distance)
            rating_norm = min(5, max(0, int(rec.get("rating", 0)))) / 5.0
            recency = _recency_factor(rec.get("created_at"), now)
            composite = (
                SIM_WEIGHT * sim
                + RATING_WEIGHT * rating_norm
                + RECENCY_WEIGHT * recency
            )
            scored.append((composite, sim, rec))
        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[dict] = []
        for composite, sim, rec in scored[:max_results]:
            result = dict(rec)
            result["_score"] = {
                "similarity": round(sim, 4),
                "composite": round(composite, 4),
            }
            out.append(result)
        # Reinforcement: every returned recipe counts as a recall. This is the
        # observable proxy for "recalled and used" — the curator (Stage 0.7
        # flag pass) treats never-recalled trajectories as consolidation
        # candidates after STALE_AFTER_DAYS.
        self._bump_recall_stats([r["id"] for r in out])
        return out

    def count(self) -> int:
        """Total indexed trajectories across all task_types."""
        return self.vec.count()

    def task_types(self) -> list[str]:
        """Sanitized task_type stems that have a JSONL file on disk."""
        return sorted(p.stem for p in self.traj_dir.glob("*.jsonl"))


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()
