"""Build (or top up) the embedding cache for the E2 recall harness.

The harness runs offline against real embedder geometry: every fixture
memory and query is embedded ONCE by the production embedder (nomic via
Ollama, with the same task prefixes the server applies) and the vectors are
stored next to the fixtures in embeddings.json. The test never talks to an
embedder; a fixture with no cached vector fails the suite and names this
script.

Usage:
    python tests/recall/embed_fixtures.py            # top up missing vectors
    python tests/recall/embed_fixtures.py --all      # re-embed everything
    MNEMO_EMBED_URL=http://host:11434 python tests/recall/embed_fixtures.py

Re-run with --all whenever the embedding model changes — the cache records
the model name and the harness refuses a cache built by a different model.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures.json"
EXPLORE_FIXTURES = HERE / "explore_fixtures.json"  # E4: explore queries over the same memories
LEXICAL_FIXTURES = HERE / "lexical_fixtures.json"  # v4.21: the lexical lane's identifier world
EMBEDDINGS = HERE / "embeddings.json"
EMBED_MODEL = "nomic-embed-text"
# Bump whenever the embed INPUT changes without a model rename (task prefixes,
# truncation): the harness refuses a cache with a different version, so stale
# geometry cannot keep the gate green.
CACHE_VERSION = 1


def cache_key(task_type: str, text: str) -> str:
    """One key per (task prefix, exact text). The prefix is part of the key
    because nomic embeds queries and documents differently."""
    return hashlib.sha256(f"{task_type}\n{text}".encode("utf-8")).hexdigest()


def load_fixtures() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def load_explore_fixtures() -> dict:
    return json.loads(EXPLORE_FIXTURES.read_text(encoding="utf-8"))


def load_lexical_fixtures() -> dict:
    return json.loads(LEXICAL_FIXTURES.read_text(encoding="utf-8"))


def load_cache() -> dict:
    if not EMBEDDINGS.exists():
        return {"model": EMBED_MODEL, "cache_version": CACHE_VERSION, "dim": None, "vectors": {}}
    return json.loads(EMBEDDINGS.read_text(encoding="utf-8"))


def wanted(fixtures: dict) -> list[tuple[str, str]]:
    docs = [("document", m["summary"]) for m in fixtures["memories"]]
    queries = [("query", q["prompt"]) for q in fixtures["queries"]]
    # E4: the explore harness asks its own questions of the same memories.
    explore = [("query", q["prompt"]) for q in load_explore_fixtures()["queries"]]
    # v4.21: the lexical world has its own memories AND queries.
    lex = load_lexical_fixtures()
    lex_pairs = [("document", m["summary"]) for m in lex["memories"]] + \
                [("query", q["prompt"]) for q in lex["queries"]]
    return docs + queries + explore + lex_pairs


async def _embed_all(pairs: list[tuple[str, str]], url: str) -> list[list[float]]:
    sys.path.insert(0, str(HERE.parents[1]))
    from agentb.providers import OllamaEmbedding, ProviderConfig
    provider = OllamaEmbedding(ProviderConfig(provider="ollama", model=EMBED_MODEL, api_base=url))
    out = []
    for i, (task_type, text) in enumerate(pairs, 1):
        out.append(await provider.embed(text, task_type=task_type))
        print(f"  {i}/{len(pairs)} {task_type:8} {text[:60]!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-embed every fixture")
    ap.add_argument("--url", default=os.environ.get("MNEMO_EMBED_URL", "http://127.0.0.1:11434"))
    args = ap.parse_args()

    fixtures = load_fixtures()
    cache = load_cache()
    if cache.get("model") != EMBED_MODEL or cache.get("cache_version") != CACHE_VERSION:
        print(f"cache is {cache.get('model')!r} v{cache.get('cache_version')}, "
              f"rebuilding for {EMBED_MODEL!r} v{CACHE_VERSION}")
        cache = {"model": EMBED_MODEL, "cache_version": CACHE_VERSION, "dim": None, "vectors": {}}
    if args.all:
        cache["vectors"] = {}

    # drop vectors for texts no longer in the fixtures so the file stays honest
    live = {cache_key(t, x) for t, x in wanted(fixtures)}
    pruned = len(cache["vectors"]) - sum(1 for k in cache["vectors"] if k in live)
    cache["vectors"] = {k: v for k, v in cache["vectors"].items() if k in live}

    todo = [(t, x) for t, x in wanted(fixtures) if cache_key(t, x) not in cache["vectors"]]
    if not todo:
        if pruned:
            EMBEDDINGS.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        print(f"cache complete: {len(cache['vectors'])} vectors, {pruned} stale pruned")
        return 0
    print(f"embedding {len(todo)} fixture texts via {args.url} ({EMBED_MODEL})")
    vectors = asyncio.run(_embed_all(todo, args.url))
    for (task_type, text), vec in zip(todo, vectors):
        if sum(v * v for v in vec) < 0.5:
            raise RuntimeError(f"embedder returned a near-zero vector for {text[:60]!r} — refusing to cache it")
        cache["vectors"][cache_key(task_type, text)] = [round(v, 7) for v in vec]
        cache["dim"] = len(vec)

    EMBEDDINGS.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {EMBEDDINGS.name}: {len(cache['vectors'])} vectors, dim {cache['dim']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
