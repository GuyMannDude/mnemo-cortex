#!/usr/bin/env python3
"""Seed Mnemo Cortex Phase 3 Facts from a YAML of canonical truth claims.

Why this exists
---------------
Semantic recall and the Facts table answer different questions. When the Facts
table is empty, a fuzzy session-log memory wins the lookup it should lose — an
agent "remembers" a stale value (an old model name, a retired port) because no
canonical Fact outranks it. Seeding the truths you already know, at
confidence=verified, gives Mnemo a hard anchor so recall stops drifting.

Idempotent: re-running skips facts whose (entity, attribute, value) already
matches. A different value at the same attribute is reported as a CONTRADICTION;
in --dry-run nothing is written, so you can inspect conflicts first.

The trap (learned the hard way)
-------------------------------
A scheduled seeder is only as truthful as its YAML. If the seed file stops
being updated, this loader will clobber your team's live corrections back to
stale values on every run — a stale "canonical truth" file is an anti-memory.
Update the YAML in the same commit as any fact fix, and treat a rising
CONTRADICTION count as "the seed is stale" until proven otherwise. See the
warning block in seed-facts.example.yaml.

Usage
-----
    seed-facts.py [--dry-run] [--yaml PATH] [--mnemo-url URL]

Environment
-----------
    MNEMO_URL          Base URL of the Mnemo Cortex service.
                       Default: http://127.0.0.1:50001 (overridden by --mnemo-url).
    MNEMO_AUTH_TOKEN   Sent as the X-API-KEY header. Only needed when the
                       service enforces auth (non-loopback binds). Omit for the
                       default loopback, no-auth setup.

YAML schema
-----------
    facts:
      - entity: workstation          # lowercase; hyphen/underscore for multi-word
        attribute: ip                 # snake_case
        value: "10.0.0.4"             # human-readable; add "NOT x" disambiguators
        confidence: verified          # optional; default verified
        evidence_source: "docs/network.md:12"   # where the truth comes from

See seed-facts.example.yaml for a worked example.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MNEMO_URL = os.environ.get("MNEMO_URL", "http://127.0.0.1:50001")


def default_yaml() -> Path:
    """Prefer a real seed-facts.yaml; fall back to the shipped example."""
    for candidate in (
        Path.cwd() / "seed-facts.yaml",
        SCRIPT_DIR / "seed-facts.yaml",
        SCRIPT_DIR / "seed-facts.example.yaml",
    ):
        if candidate.exists():
            return candidate
    return SCRIPT_DIR / "seed-facts.example.yaml"


def load_facts(yaml_path: Path) -> list[dict]:
    with yaml_path.open() as f:
        data = yaml.safe_load(f) or {}
    facts = data.get("facts", [])
    for i, fact in enumerate(facts):
        for required in ("entity", "attribute", "value", "evidence_source"):
            if required not in fact:
                raise ValueError(f"fact #{i} missing required field: {required}")
        # Facts values are strings; coerce so an unquoted YAML number/bool
        # (port: 50001) doesn't break the API payload or the [:80] preview.
        fact["value"] = str(fact["value"])
    return facts


def auth_headers() -> dict:
    token = os.environ.get("MNEMO_AUTH_TOKEN", "").strip()
    return {"X-API-KEY": token} if token else {}


def fetch_existing(client: httpx.Client, entity: str, attribute: str) -> dict | None:
    r = client.get(f"/facts/{entity}/{attribute}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    if body.get("found") is False:
        return None
    return body


def save_fact(client: httpx.Client, fact: dict) -> dict:
    payload = {
        "entity": fact["entity"],
        "attribute": fact["attribute"],
        "value": fact["value"],
        "confidence": fact.get("confidence", "verified"),
        "evidence_source": fact["evidence_source"],
    }
    r = client.post("/facts", json=payload)
    r.raise_for_status()
    return r.json()


def classify(fact: dict, existing: dict | None) -> str:
    if existing is None:
        return "NEW"
    existing_fact = existing.get("fact", existing)
    if existing_fact.get("value") == fact["value"]:
        return "MATCH"
    return "CONTRADICTION"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed Mnemo Cortex Phase 3 Facts from a YAML of canonical claims."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen, don't POST anything.")
    parser.add_argument("--yaml", type=Path, default=None,
                        help="Source YAML (default: ./seed-facts.yaml, then the shipped example).")
    parser.add_argument("--mnemo-url", default=DEFAULT_MNEMO_URL,
                        help=f"Mnemo Cortex base URL (default: {DEFAULT_MNEMO_URL}).")
    args = parser.parse_args()

    yaml_path = args.yaml or default_yaml()
    if not yaml_path.exists():
        print(f"ERROR: YAML not found: {yaml_path}", file=sys.stderr)
        return 2

    print(f"YAML:      {yaml_path}")
    print(f"Mnemo URL: {args.mnemo_url}")
    print(f"Auth:      {'X-API-KEY (token set)' if auth_headers() else 'none (loopback)'}")
    print(f"Mode:      {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print()

    facts = load_facts(yaml_path)
    print(f"Loaded {len(facts)} facts.\n")

    counts = {"NEW": 0, "MATCH": 0, "CONTRADICTION": 0, "ERROR": 0}

    with httpx.Client(base_url=args.mnemo_url.rstrip("/"),
                      headers=auth_headers(), timeout=10) as client:
        for fact in facts:
            eid = f"{fact['entity']}.{fact['attribute']}"
            try:
                existing = fetch_existing(client, fact["entity"], fact["attribute"])
            except httpx.HTTPError as e:
                print(f"  [ERROR fetch] {eid}: {e}")
                counts["ERROR"] += 1
                continue

            verdict = classify(fact, existing)
            counts[verdict] += 1

            if verdict == "MATCH":
                print(f"  [MATCH]    {eid}")
                continue

            if verdict == "CONTRADICTION":
                existing_fact = existing.get("fact", existing) if existing else {}
                print(f"  [CONFLICT] {eid}")
                print(f"             existing: {existing_fact.get('value')!r} ({existing_fact.get('confidence')})")
                print(f"             new:      {fact['value']!r} ({fact.get('confidence', 'verified')})")

            if verdict == "NEW":
                print(f"  [NEW]      {eid} = {fact['value'][:80]}")

            if not args.dry_run:
                try:
                    result = save_fact(client, fact)
                    if result.get("proposal_id") is not None:
                        print(f"             → HELD by lock ({result.get('authority')}): {result.get('reason')}")
                    elif result.get("was_contradiction") and result.get("written"):
                        print(f"             → overwrote {result.get('previous_value')!r} ({result.get('previous_confidence')})")
                    elif result.get("was_contradiction"):
                        print(f"             → rejected: {result.get('reason')}")
                except httpx.HTTPError as e:
                    print(f"  [ERROR save] {eid}: {e}")
                    counts["ERROR"] += 1

    print()
    print("─" * 60)
    print(f"  NEW:           {counts['NEW']}")
    print(f"  MATCH:         {counts['MATCH']}")
    print(f"  CONTRADICTION: {counts['CONTRADICTION']}")
    print(f"  ERROR:         {counts['ERROR']}")
    print("─" * 60)

    if args.dry_run and (counts["NEW"] + counts["CONTRADICTION"]) > 0:
        print("\nRe-run without --dry-run to commit changes.")

    return 0 if counts["ERROR"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
