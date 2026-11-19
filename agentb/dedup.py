"""Bounded, explainable near-duplicate detection for memory writeback."""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from pathlib import Path

from agentb.cache import cosine_similarity
from agentb.vec import VecStore, is_superseded, record_file

STOP = set("""a an the of to in on for at by with as is are was be been it its
this that these those and or so then than there here from into onto over under
you your our we i my me""".split())


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix) and not word.endswith("ss"):
            word = word[:-len(suffix)]
            break
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    words = re.findall(r"[a-z0-9]+", normalized)
    return {stem(word) for word in words if word not in STOP and len(word) > 1}


def overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / min(len(left), len(right)) if left and right else 0.0


def load_allowlist(path: Path) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pairs
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "|" in line:
            a, b = (part.strip() for part in line.split("|", 1))
            if a and b:
                pairs.add(frozenset((a, b)))
        elif line:
            pairs.add(frozenset(("*", line)))
    return pairs


def _search(db_path: Path, embedding: list[float], text: str, candidate_id: str,
            allow_path: Path, top_k: int, cosine_threshold: float,
            overlap_threshold: float, *, memory_dir: Path | None) -> list[dict]:
    # An isolated connection keeps SQLite work off the request loop without
    # sharing a connection across threads.
    store = VecStore(db_path)
    try:
        allow = load_allowlist(allow_path)
        query_tokens = tokens(text)
        now = time.time()
        matches = []
        live = 0
        # Over-fetch so hits dropped below do not cost live candidates; still
        # look at no more than top_k live ones. A demoted memory whose vector
        # is back, or a row whose record is gone (item 14 A), must not hold a
        # new save or be quoted as its twin.
        for hit in store.search(embedding, top_k=top_k * 2):
            if live >= top_k:
                break
            record = record_file(hit.source_file, memory_dir)
            if (hit.source_file and record is None) or is_superseded(record):
                continue
            live += 1
            if (frozenset((candidate_id, hit.memory_id)) in allow
                    or frozenset(("*", hit.memory_id)) in allow):
                continue
            known_embedding = store.get_embedding(hit.memory_id)
            if not known_embedding:
                continue
            cosine = cosine_similarity(embedding, known_embedding)
            hit_tokens = tokens(hit.text)
            token_overlap = overlap(query_tokens, hit_tokens)
            if cosine < cosine_threshold or token_overlap < overlap_threshold:
                continue
            age = (now - hit.created_at) / 86400.0 if hit.created_at else None
            matches.append({
                "id": hit.memory_id,
                "age_days": round(age, 1) if age is not None else None,
                "cosine": round(cosine, 4),
                "overlap": round(token_overlap, 4),
                "shared_tokens": sorted(query_tokens & hit_tokens),
                "excerpt": hit.text.replace("\n", " ")[:240],
            })
        return matches
    finally:
        store.close()


async def find_near_duplicates(*, vec_store: VecStore, embedding: list[float],
                               text: str, candidate_id: str, allow_path: Path,
                               top_k: int, cosine_threshold: float,
                               overlap_threshold: float, min_tokens: int,
                               memory_dir: Path | None) -> list[dict]:
    # memory_dir is required on purpose: without it a data dir that moved
    # reads every hit as an orphan, and the gate is silently off (14 A).
    if len(tokens(text)) < min_tokens:
        return []
    return await asyncio.to_thread(
        _search, vec_store.db_path, embedding, text, candidate_id, allow_path,
        top_k, cosine_threshold, overlap_threshold, memory_dir=memory_dir)
