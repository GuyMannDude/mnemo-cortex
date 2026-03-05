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
    def __init__(self, content: str, source: str, relevance: float, cache_tier: str):
        self.content = content
        self.source = source
        self.relevance = relevance
        self.cache_tier = cache_tier

    def to_dict(self) -> dict:
        return {"content": self.content, "source": self.source,
                "relevance": round(self.relevance, 4), "cache_tier": self.cache_tier}


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
        return [ContextChunk(b["content"], b.get("source", "l1-cache"), s, "L1")
                for s, b in scored[:top_k]]

    async def add(self, content: str, source: str, embedding: list[float]) -> str:
        bundle_id = hashlib.sha256(content.encode()).hexdigest()[:12]
        bundle = {"id": bundle_id, "content": content, "source": source,
                  "embedding": embedding, "created_at": time.time()}
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
        threshold = self.config.l2_similarity_threshold
        if persona and persona.l2_similarity_override is not None:
            threshold = persona.l2_similarity_override

        scored = []
        for entry in self.entries:
            if not entry.get("embedding"):
                continue
            sim = cosine_similarity(query_embedding, entry["embedding"])
            if sim > threshold:
                scored.append((sim, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [ContextChunk(e["content"], e.get("source", "l2-memory"), s, "L2")
                for s, e in scored[:top_k]]

    async def add(self, content: str, source: str, embedding: list[float],
                  metadata: dict = None) -> str:
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
) -> list[ContextChunk]:
    memory_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for mem_file in sorted(memory_dir.glob("*.json")):
        try:
            mem = json.loads(mem_file.read_text())
            content = mem.get("summary", "") + "\n" + "\n".join(mem.get("key_facts", []))
            if not content.strip():
                continue
            content_embedding = await embed_fn(content)
            sim = cosine_similarity(query_embedding, content_embedding)
            if sim > threshold:
                results.append(ContextChunk(content, f"l3-scan:{mem_file.stem}", sim, "L3"))
        except Exception as e:
            log.warning(f"L3 error {mem_file}: {e}")
    results.sort(key=lambda x: x.relevance, reverse=True)
    return results[:top_k]
