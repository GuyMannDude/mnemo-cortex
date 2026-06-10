"""
AgentB Cache Hierarchy v0.3.0
L1/L2/L3 with persona-aware similarity thresholds.
"""

import json
import time
import hashlib
import logging
from pathlib import Path
from typing import Optional, Callable, Awaitable

import numpy as np

from agentb.config import CacheConfig, PersonaConfig

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

    def to_dict(self) -> dict:
        d = {"content": self.content, "source": self.source,
             "relevance": round(self.relevance, 4), "cache_tier": self.cache_tier}
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
        mem = json.loads(mem_path.read_text())
    except Exception:
        return chunk
    from agentb.provenance import compute_stale_warning
    chunk.category = mem.get("category")
    chunk.provenance_source = mem.get("source")
    created_at = mem.get("created_at")
    if created_at:
        chunk.age_days = round((time.time() - float(created_at)) / 86400.0, 1)
        chunk.stale_warning = compute_stale_warning(chunk.category, created_at) if chunk.category else None
    return chunk


class L1Cache:
    def __init__(self, cache_dir: Path, config: CacheConfig):
        self.cache_dir = cache_dir
        self.config = config
        self.bundles: list[dict] = []
        self._load()

    def _load(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.bundles = []
        for f in sorted(self.cache_dir.glob("*.json")):
            try:
                self.bundles.append(json.loads(f.read_text()))
            except Exception as e:
                log.warning(f"L1 load error {f}: {e}")
        log.info(f"L1 cache: {len(self.bundles)} bundles")

    def search(self, query_embedding: list[float], top_k: int = 3,
               persona: Optional[PersonaConfig] = None) -> list[ContextChunk]:
        threshold = self.config.l1_similarity_threshold
        if persona and persona.l1_similarity_override is not None:
            threshold = persona.l1_similarity_override

        now = time.time()
        scored = []
        for bundle in self.bundles:
            age = now - bundle.get("created_at", 0)
            if age > self.config.l1_ttl_seconds:
                continue
            if not bundle.get("embedding"):
                continue
            sim = cosine_similarity(query_embedding, bundle["embedding"])
            if sim >= threshold:
                scored.append((sim, bundle))

        scored.sort(key=lambda x: x[0], reverse=True)
        # v4.0.2: carry memory_id + category so /context can disk-truth-validate
        # the category filter. An L1 bundle with no memory_id can't be tied back
        # to its memory JSON, so session_log leaked past the filter.
        return [ContextChunk(b["content"], b.get("source", "l1-cache"), s, "L1",
                             memory_id=b.get("memory_id"), category=b.get("category"),
                             created_at=b.get("created_at"))
                for s, b in scored[:top_k]]

    async def add(self, content: str, source: str, embedding: list[float],
                  memory_id: Optional[str] = None, category: Optional[str] = None) -> str:
        bundle_id = hashlib.sha256(content.encode()).hexdigest()[:12]
        bundle = {"id": bundle_id, "content": content, "source": source,
                  "embedding": embedding, "created_at": time.time(),
                  "memory_id": memory_id, "category": category}
        self.bundles.append(bundle)
        if len(self.bundles) > self.config.l1_max_bundles:
            self.bundles.sort(key=lambda b: b.get("created_at", 0))
            evicted = self.bundles.pop(0)
            (self.cache_dir / f"{evicted['id']}.json").unlink(missing_ok=True)
        (self.cache_dir / f"{bundle_id}.json").write_text(json.dumps(bundle, default=str))
        return bundle_id

    @property
    def size(self) -> int:
        return len(self.bundles)


class L2Index:
    def __init__(self, index_dir: Path, config: CacheConfig):
        self.index_dir = index_dir
        self.config = config
        self.entries: list[dict] = []
        self._load()

    def _load(self):
        self.index_dir.mkdir(parents=True, exist_ok=True)
        index_file = self.index_dir / "index.json"
        if index_file.exists():
            try:
                self.entries = json.loads(index_file.read_text())
                log.info(f"L2 index: {len(self.entries)} entries")
            except Exception as e:
                log.warning(f"L2 load error: {e}")

    def _save(self):
        (self.index_dir / "index.json").write_text(json.dumps(self.entries, default=str))

    def search(self, query_embedding: list[float], top_k: int = 5,
               persona: Optional[PersonaConfig] = None) -> list[ContextChunk]:
        from agentb.provenance import compute_stale_warning

        threshold = self.config.l2_similarity_threshold
        if persona and persona.l2_similarity_override is not None:
            threshold = persona.l2_similarity_override

        now = time.time()
        scored = []
        for entry in self.entries:
            if not entry.get("embedding"):
                continue
            sim = cosine_similarity(query_embedding, entry["embedding"])
            if sim > threshold:
                scored.append((sim, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[ContextChunk] = []
        for s, e in scored[:top_k]:
            meta = e.get("metadata") or {}
            created_at = e.get("created_at")
            age_days = None
            if created_at:
                age_days = round((now - float(created_at)) / 86400.0, 1)
            category = meta.get("category")
            stale = compute_stale_warning(category, created_at) if category else None
            out.append(ContextChunk(
                e["content"], e.get("source", "l2-memory"), s, "L2",
                memory_id=meta.get("memory_id"),
                provenance_source=meta.get("provenance_source"),
                category=category,
                additional_tags=meta.get("additional_tags") or [],
                age_days=age_days,
                stale_warning=stale,
                created_at=created_at,
            ))
        return out

    async def add(self, content: str, source: str, embedding: list[float],
                  metadata: Optional[dict] = None) -> str:
        entry_id = hashlib.sha256(content.encode()).hexdigest()[:12]
        self.entries.append({"id": entry_id, "content": content, "source": source,
                            "embedding": embedding, "metadata": metadata or {},
                            "created_at": time.time()})
        self._save()
        return entry_id

    @property
    def size(self) -> int:
        return len(self.entries)


async def l3_scan(
    memory_dir: Path,
    query_embedding: list[float],
    embed_fn: Callable[[str], Awaitable[list[float]]],
    threshold: float = 0.4,
    top_k: int = 3,
    prefilter: Optional[Callable[..., bool]] = None,
) -> list[ContextChunk]:
    from agentb.provenance import compute_stale_warning

    memory_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    results = []
    for mem_file in sorted(memory_dir.glob("*.json")):
        try:
            mem = json.loads(mem_file.read_text())
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
            content_embedding = await embed_fn(content)
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
