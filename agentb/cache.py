"""
Mnemo Cortex recall helpers: ContextChunk, disk-truth resolution and the L3
disk-walk escape hatch. The L1 bundle cache and L2 index that lived here were
retired from recall in E3 and deleted in its follow-up (2026-09).
"""

import asyncio
import json
import time
import logging
from pathlib import Path
from typing import Optional, Callable, Awaitable

import numpy as np


log = logging.getLogger("agentb.cache")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr = np.array(a, dtype=np.float32)
    b_arr = np.array(b, dtype=np.float32)
    dot = np.dot(a_arr, b_arr)
    norm = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    return float(dot / norm) if norm > 0 else 0.0


class ContextChunk:
    def __init__(
        self,
        content: str,
        source: str,
        relevance: float,
        cache_tier: str,
        *,
        memory_id: Optional[str] = None,
        provenance_source: Optional[str] = None,
        category: Optional[str] = None,
        additional_tags: Optional[list] = None,
        age_days: Optional[float] = None,
        stale_warning: Optional[dict] = None,
        created_at: Optional[float] = None,
        revises: Optional[list] = None,
        lexical: float = 0.0,
        exact: bool = False,
    ):
        self.content = content
        self.source = source
        self.relevance = relevance
        self.cache_tier = cache_tier
        # memory_id ties chunks across tiers (set when the chunk traces back
        # to a writeback record); enables cross-tier dedup.
        self.memory_id = memory_id
        # v3 fields (all optional — pre-v3 chunks leave them None)
        self.provenance_source = provenance_source
        self.category = category
        self.additional_tags = additional_tags or []
        self.age_days = age_days
        self.stale_warning = stale_warning
        self.created_at = created_at
        # v4.18.4: ids this memory revises — the near-duplicates a writeback
        # was held against and the caller then FORCED past (`near_dup_of`
        # with `near_dup_forced`). Read by ranking.order_revisions.
        self.revises = revises or []
        # v4.21: the lexical lane's BM25 evidence for this memory, scaled to
        # the OR-query's best hit (0.0 = the lane did not find it; an exact
        # pass hit can exceed 1.0, composite_score clamps). Not on the wire.
        self.lexical = lexical
        # v4.21.1: this memory holds a rare identifier the prompt named
        # (vec.exact_terms). Served first in focus and recent modes.
        self.exact = exact

    def to_dict(self) -> dict:
        d = {"content": self.content, "source": self.source,
             "relevance": round(self.relevance, 4), "cache_tier": self.cache_tier}
        if self.memory_id is not None:
            d["memory_id"] = self.memory_id
        if self.provenance_source is not None:
            d["provenance_source"] = self.provenance_source
        if self.category is not None:
            d["category"] = self.category
        if self.additional_tags:
            d["additional_tags"] = self.additional_tags
        if self.age_days is not None:
            d["age_days"] = self.age_days
        if self.stale_warning is not None:
            d["stale_warning"] = self.stale_warning
        return d


def resolve_disk_truth(chunk: ContextChunk, memory_dir: Path) -> Optional[ContextChunk]:
    """Re-read a chunk's canonical category/source from its memory JSON on disk.

    L1/L2 cache the category at write time; the v4.0 reclassification migration
    rewrote only the on-disk memory files, leaving those caches stale or empty —
    so session_log leaked past the /context category filter (which treats
    category=None as "do not exclude"). Mutates the chunk in place with disk-truth
    metadata, mirroring the v4.0.1 VEC-tier fix, so the filter sees the same
    category the L3 disk-walk would.

    v4.1 contract changes:
      - memory_id present but JSON gone → the memory was DELETED (purge sweep,
        migration). Returns None so the caller drops it — the June-9 dedup sweep
        purged [AUTO-CAPTURE] rows from vec + disk, yet they kept resurfacing
        through the L2 cache because this used to no-op.
      - no memory_id at all (legacy pre-v3 cache entry) → the content itself is
        the only signal; auto-capture/auto-sync shapes get tagged session_log so
        the default two-tier hiding finally applies to them.
    """
    if not chunk.memory_id:
        from agentb.classify import is_routine_log
        if is_routine_log(chunk.content, None):
            chunk.category = "session_log"
        return chunk
    mem_path = memory_dir / f"{chunk.memory_id}.json"
    if not mem_path.exists():
        return None
    try:
        mem = json.loads(mem_path.read_text(encoding="utf-8"))
    except Exception:
        return chunk
    if mem.get("superseded_by"):
        return None
    from agentb.provenance import compute_stale_warning
    chunk.category = mem.get("category")
    chunk.provenance_source = mem.get("source")
    created_at = mem.get("created_at")
    if created_at:
        chunk.age_days = round((time.time() - float(created_at)) / 86400.0, 1)
        chunk.stale_warning = compute_stale_warning(chunk.category, created_at) if chunk.category else None
    return chunk


async def l3_scan(
    memory_dir: Path,
    query_embedding: list[float],
    embed_fn: Callable[[str], Awaitable[list[float]]],
    threshold: float = 0.4,
    top_k: int = 3,
    prefilter: Optional[Callable[..., bool]] = None,
    max_candidates: Optional[int] = None,
) -> list[ContextChunk]:
    from agentb.provenance import compute_stale_warning

    memory_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    results = []

    # v4.1.1: walk newest-first and cap the number of EMBEDS (max_candidates).
    # L3 embeds every prefilter-passing file — O(store size) ollama calls — which
    # blows the bridge timeout on a large session_log-dominated store. Recency
    # order means the bounded sample keeps the most-recent (usually most-relevant)
    # candidates instead of an arbitrary filename-hash slice. None = uncapped
    # (legacy callers / small stores). Cheap reads (json.loads, prefilter) are NOT
    # capped — only the expensive embed is.
    def _collect_candidates() -> list[tuple]:
        # The whole disk walk (O(store) stat calls + file reads + prefilter)
        # runs off the event loop: on a 6.2k-file store this section alone
        # stalled every concurrent request — including /health — for seconds.
        out = []
        files = sorted(memory_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for mem_file in files:
            try:
                mem = json.loads(mem_file.read_text(encoding="utf-8"))
                if mem.get("superseded_by"):
                    continue
                content = mem.get("summary", "") + "\n" + "\n".join(mem.get("key_facts", []))
                if not content.strip():
                    continue
                # Compute metadata from disk *before* the expensive embed. A
                # category / source / age / stale filter prunes here so we never
                # pay to embed a candidate we'd only discard. Before this, a
                # category-filtered cross-agent recall embedded ~every file
                # (~17 sequential embed calls/request → MCP-bridge timeout).
                created_at = mem.get("created_at")
                age_days = round((now - float(created_at)) / 86400.0, 1) if created_at else None
                category = mem.get("category")
                stale = compute_stale_warning(category, created_at) if category else None
                source = mem.get("source")
                if prefilter is not None and not prefilter(
                    source=source, category=category, age_days=age_days, stale_warning=stale
                ):
                    continue
                out.append((mem_file, mem, content, created_at, age_days,
                            category, stale, source))
            except Exception as e:
                log.warning(f"L3 error {mem_file}: {e}")
        return out

    candidates = await asyncio.to_thread(_collect_candidates)

    embedded = 0
    for (mem_file, mem, content, created_at, age_days,
         category, stale, source) in candidates:
        if max_candidates is not None and embedded >= max_candidates:
            break
        try:
            content_embedding = await embed_fn(content)
            embedded += 1
            sim = cosine_similarity(query_embedding, content_embedding)
            if sim > threshold:
                results.append(ContextChunk(
                    content, f"l3-scan:{mem_file.stem}", sim, "L3",
                    memory_id=mem.get("id") or mem_file.stem,
                    provenance_source=source,
                    category=category,
                    additional_tags=mem.get("additional_tags") or [],
                    age_days=age_days,
                    stale_warning=stale,
                    created_at=created_at,
                ))
        except Exception as e:
            log.warning(f"L3 error {mem_file}: {e}")
    results.sort(key=lambda x: x.relevance, reverse=True)
    return results[:top_k]
