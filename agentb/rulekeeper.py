"""Read-only nightly near-duplicate advisory scan."""
from __future__ import annotations
import json
import time
from pathlib import Path
from agentb.cache import cosine_similarity
from agentb.dedup import load_allowlist, overlap, tokens
from agentb.vec import VecStore, is_superseded, record_file


def scan_tenant(memory_dir: Path, vec_store: VecStore, *, window_days: int = 7,
                top_k: int = 5, cosine_threshold: float = 0.80,
                overlap_threshold: float = 0.55, min_tokens: int = 5,
                report_max_pairs: int = 10) -> dict:
    corpus_size = vec_store.count()
    source = {"thresholds": {"cosine": cosine_threshold, "overlap": overlap_threshold,
                             "min_tokens": min_tokens}, "top_k": top_k,
              "corpus_size": corpus_size, "window_days": window_days}
    if corpus_size == 0:
        return {"status": "blind", "exit_code": 2, "source": source, "pairs": []}
    cutoff = time.time() - window_days * 86400
    allow = load_allowlist(memory_dir.parent / "dedup-allow.txt")
    pairs, seen = [], set()
    for path in memory_dir.glob("*.json"):
        try:
            memory = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # The window is about ARRIVAL, not birth: a backdated import must be
        # scanned in the week it lands. ingested_at since v4.18.3; older
        # records only carry created_at, which for them equalled arrival.
        arrived = memory.get("ingested_at") or memory.get("created_at") or 0
        if float(arrived) < cutoff or memory.get("superseded_by"):
            continue
        memory_id = memory.get("id") or path.stem
        embedding = vec_store.get_embedding(memory_id)
        text = memory.get("summary", "") + "\n" + "\n".join(memory.get("key_facts") or [])
        query_tokens = tokens(text)
        if not embedding or len(query_tokens) < min_tokens:
            continue
        # Over-fetch: the record itself, demoted hits (a vector back in the
        # index) and rows whose record is gone (14 C) are skipped, and no more
        # than top_k live ones are looked at. A moved data dir finds by name.
        live = 0
        for hit in vec_store.search(embedding, top_k=(top_k + 1) * 2):
            record = record_file(hit.source_file, memory_dir)
            if (hit.memory_id == memory_id or (hit.source_file and record is None)
                    or is_superseded(record)):
                continue
            live += 1
            if live > top_k:
                break
            pair_key = frozenset((memory_id, hit.memory_id))
            if (pair_key in seen or pair_key in allow
                    or frozenset(("*", hit.memory_id)) in allow):
                continue
            seen.add(pair_key)
            known = vec_store.get_embedding(hit.memory_id)
            if not known:
                continue
            cosine = cosine_similarity(embedding, known)
            token_overlap = overlap(query_tokens, tokens(hit.text))
            if cosine >= cosine_threshold and token_overlap >= overlap_threshold:
                pairs.append({"new_id": memory_id, "existing_id": hit.memory_id,
                              "cosine": round(cosine, 4),
                              "overlap": round(token_overlap, 4),
                              "new_home": str(path), "existing_home": hit.source_file})
    pair_count = len(pairs)
    pairs.sort(key=lambda pair: (pair["overlap"], pair["cosine"]), reverse=True)
    reported = pairs[:max(0, report_max_pairs)]
    return {"status": "duplicates" if pair_count else "clean",
            "exit_code": 1 if pair_count else 0, "source": source,
            "pair_count": pair_count, "pairs": reported}


def format_advisory(agent_id: str, result: dict) -> str:
    source = result["source"]
    lines = [f"## Rulekeeper advisory - {agent_id}",
             f"Source: corpus={source['corpus_size']} window={source['window_days']}d "
             f"top-k={source['top_k']} cosine>={source['thresholds']['cosine']:.2f} "
             f"overlap>={source['thresholds']['overlap']:.2f}"]
    if result["status"] == "blind":
        lines.append("BLIND STORE: zero indexed memories; clean cannot be asserted.")
    elif not result["pairs"]:
        lines.append("No unallowlisted near-duplicate pairs found.")
    else:
        lines.append(f"Advisory only: {result.get('pair_count', len(result['pairs']))} pair(s); "
                     f"showing {len(result['pairs'])}; nothing merged or demoted.")
        for pair in result["pairs"]:
            lines.append(f"- {pair['new_id']} ~ {pair['existing_id']} "
                         f"cosine={pair['cosine']:.4f} overlap={pair['overlap']:.4f} "
                         f"homes={pair['new_home']} | {pair['existing_home']}")
    return "\n".join(lines)
