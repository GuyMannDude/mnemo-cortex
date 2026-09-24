#!/usr/bin/env python3
"""
Jev shadow classifier — offline spike (idea 55a1f366, go from Guy 2026-09-23).

Re-classifies memories that Mnemo already categorised with TypeSafe's Jev, a
System One model that returns a probability per category instead of a bare
word. Nothing here touches the live save path: it reads a vec_index.sqlite
(the hourly corpus backup on IGOR is fine, opened immutable), calls Jev, and
writes one JSONL row per memory plus a summary.

The reference label is the STORED category, which is mixed provenance: the
LLM classifier (Gemini Flash via `agentb.classify`), the regex fallback when
the LLM was down, an explicit caller category, or an analyst lens. The index
carries no `classified_by`, so the summary cannot split them. Read "agreement"
as agreement with what Mnemo holds today, not as agreement with Gemini.

What we learn: agreement with the stored category, per-category confusion,
and whether Jev's confidence separates its agreements from its disagreements
(the calibration question). Disagreements are the rows worth a human look.

Key: TYPESAFE_API_KEY in the environment, or --key-file <path>. Never printed.

The LIVE server copy of build_request/parse_response is agentb/jev_live.py
(4.26.0) — edit that one for the server; this file is the offline spike.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from agentb.classify import CLASSIFIABLE_CATEGORIES, DEFAULT_MAX_INPUT_CHARS  # noqa: E402

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# Tier-1 targets only. session_log is the free noise heuristic's job and Jev
# should never be asked about it — same exclusion the LLM prompt makes.
CRITERIA = {name: desc for name, desc in CLASSIFIABLE_CATEGORIES if name != "session_log"}
INSTRUCTIONS = "Which single category best fits this memory for a memory system?"
DEFAULT_DB = Path("/media/guy/SPARKSVAULT/mnemo-backups/latest/agents/cc/vec_index.sqlite")
DEFAULT_OUT_DIR = Path.home() / ".agentb" / "jev-shadow"
RETRY_STATUSES = {429, 529}
FATAL_STATUSES = {401, 403}  # bad or under-scoped key: never retry, never continue
MAX_CONSECUTIVE_ERRORS = 10
PRICE_PER_M_INPUT_USD = 0.042  # typesafe.ai/blog launch post, read 2026-09-23; output free


def build_request(text: str, max_chars: int = DEFAULT_MAX_INPUT_CHARS) -> dict:
    """The exact JSON Jev gets. Same input cap as the live classifier."""
    return {
        "state": text.strip()[:max_chars],
        "model": MODEL,
        "questions": {
            "category": {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": CRITERIA,
            }
        },
    }


def parse_response(body: dict) -> dict:
    """Pull the fields we keep. Raises KeyError on an unexpected shape —
    the caller records that as an error row rather than guessing."""
    ans = body["answers"]["category"]
    probs = {k: float(v) for k, v in ans["probabilities"].items()}
    ranked = sorted(probs.values(), reverse=True)
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
    return {
        "jev_choice": ans["choice"],
        "confidence": float(ans["confidence"]),
        "probabilities": probs,
        "top2_margin": round(margin, 4),
        "jev_model": body.get("model"),
        "input_tokens": (body.get("usage") or {}).get("input_tokens"),
    }


def sample_memories(db: Path, per_category: int, seed: int) -> list[dict]:
    """Stratified random sample of Tier-1 memories from a vec_index.sqlite.

    immutable=1: the index is WAL-mode, and a plain mode=ro open still drops
    -wal/-shm sidecars beside a backup copy. Immutable takes no locks and
    writes nothing; correct for a static snapshot, never for a live index.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rows = con.execute(
        "SELECT memory_id, text, category FROM vec_sources WHERE category IN (%s) "
        "ORDER BY memory_id" % ",".join("?" * len(CRITERIA)),
        tuple(CRITERIA),
    ).fetchall()
    con.close()
    by_cat: dict[str, list] = defaultdict(list)
    for mid, text, cat in rows:
        by_cat[cat].append({"memory_id": mid, "text": text, "stored_category": cat})
    rng = random.Random(seed)
    out: list[dict] = []
    for cat in CRITERIA:
        pool = by_cat.get(cat, [])
        rng.shuffle(pool)
        out.extend(pool[:per_category])
    rng.shuffle(out)
    return out


def call_jev(client: httpx.Client, payload: dict, retries: int = 5) -> tuple[dict, float]:
    """POST with backoff on 429/529. 401/403 are fatal: a bad key never retries.

    Other 4xx/5xx raise HTTPStatusError with the response body appended so a
    wrong request shape is explained, not just counted. The body never echoes
    request headers, so the key stays out of the error string.
    """
    delay = 1.0
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        r = client.post(API_URL, json=payload)
        latency = (time.monotonic() - t0) * 1000
        if r.status_code in FATAL_STATUSES:
            raise SystemExit(f"Jev: HTTP {r.status_code} — key rejected. Stopping.")
        if r.status_code in RETRY_STATUSES and attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise httpx.HTTPStatusError(
                f"{e} :: {r.text[:150]}", request=e.request, response=e.response
            ) from None
        return r.json(), latency
    raise RuntimeError("unreachable: last attempt returns or raises")


def summarize(rows: list[dict]) -> dict:
    """Agreement, confusion, and calibration buckets over the scored rows."""
    ok = [r for r in rows if "jev_choice" in r]
    errors = len(rows) - len(ok)
    if not ok:
        return {"scored": 0, "errors": errors}
    agree = [r for r in ok if r["jev_choice"] == r["stored_category"]]
    per_cat: dict[str, dict] = {}
    for cat in CRITERIA:
        sub = [r for r in ok if r["stored_category"] == cat]
        if sub:
            per_cat[cat] = {
                "n": len(sub),
                "agree_rate": round(sum(r["jev_choice"] == cat for r in sub) / len(sub), 3),
            }
    confusion: Counter = Counter(
        (r["stored_category"], r["jev_choice"]) for r in ok if r["jev_choice"] != r["stored_category"]
    )
    buckets: dict[str, list] = defaultdict(list)
    for r in ok:
        lo = min(int(r["confidence"] * 10), 9) / 10  # confidence 1.0 lands in 0.9-1.0
        buckets[f"{lo:.1f}-{lo + 0.1:.1f}"].append(r["jev_choice"] == r["stored_category"])
    calibration = {
        k: {"n": len(v), "agree_rate": round(sum(v) / len(v), 3)} for k, v in sorted(buckets.items())
    }
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    disagree = [r for r in ok if r["jev_choice"] != r["stored_category"]]
    tokens = [r["input_tokens"] for r in ok if r.get("input_tokens")]
    return {
        "reference": "stored category (mixed provenance: llm / regex / caller / lens)",
        "scored": len(ok),
        "errors": errors,
        "agree_rate": round(len(agree) / len(ok), 3),
        "mean_confidence_agree": mean([r["confidence"] for r in agree]),
        "mean_confidence_disagree": mean([r["confidence"] for r in disagree]),
        "mean_latency_ms": mean([r["latency_ms"] for r in ok]),
        "per_category": per_cat,
        "top_confusions": [
            {"stored": s, "jev": j, "n": n} for (s, j), n in confusion.most_common(10)
        ],
        "calibration": calibration,
        "input_tokens_total": sum(tokens),
        "est_cost_usd_at_0.042_per_M_input": round(sum(tokens) / 1_000_000 * PRICE_PER_M_INPUT_USD, 4),
    }


def load_key(key_file: str | None) -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if key_file:
        lines = Path(key_file).read_text(encoding="utf-8").strip().splitlines()
        key = lines[-1].strip() if lines else ""
    if not key:
        raise SystemExit("No key: set TYPESAFE_API_KEY or pass --key-file. Nothing sent.")
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="vec_index.sqlite to sample from")
    ap.add_argument("--per-category", type=int, default=25, help="memories per stored category")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--key-file", help="file whose last line is the API key (USB drop)")
    ap.add_argument("--out", type=Path, help="JSONL path (default ~/.agentb/jev-shadow/<stamp>.jsonl)")
    ap.add_argument("--dry-run", action="store_true", help="build the sample and print one request; no network")
    args = ap.parse_args(argv)

    sample = sample_memories(args.db, args.per_category, args.seed)
    print(f"sampled {len(sample)} memories from {args.db}", file=sys.stderr)
    if not sample:
        print("nothing to classify — wrong db or no Tier-1 rows", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps(build_request(sample[0]["text"]), indent=2)[:2500])
        print(json.dumps(Counter(r["stored_category"] for r in sample), indent=1), file=sys.stderr)
        return 0

    key = load_key(args.key_file)
    out = args.out or DEFAULT_OUT_DIR / time.strftime("jev-shadow-%Y%m%d-%H%M%S.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    consecutive_errors = 0
    with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30) as client, \
            out.open("w", encoding="utf-8") as fh:
        for i, m in enumerate(sample, 1):
            row = {"memory_id": m["memory_id"], "stored_category": m["stored_category"],
                   "text_head": m["text"][:120]}
            try:
                body, latency = call_jev(client, build_request(m["text"]))
                row.update(parse_response(body))
                row["latency_ms"] = round(latency, 1)
                row["agree"] = row["jev_choice"] == row["stored_category"]
                consecutive_errors = 0
            except Exception as e:  # one bad row never ends the batch; ten in a row do
                row["error"] = f"{type(e).__name__}: {e}"[:300]
                consecutive_errors += 1
            rows.append(row)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"{consecutive_errors} errors in a row, last: {row['error']}", file=sys.stderr)
                break
            if i % 25 == 0 or i == len(sample):
                scored = sum("jev_choice" in r for r in rows)
                print(f"{i}/{len(sample)} sent, {scored} scored", file=sys.stderr)

    summary = summarize(rows)
    summary["db"] = str(args.db)
    summary["db_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(args.db.stat().st_mtime))
    summary["seed"] = args.seed
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"rows: {out}\nsummary: {summary_path}", file=sys.stderr)
    return 0 if summary.get("scored") else 1


if __name__ == "__main__":
    sys.exit(main())
