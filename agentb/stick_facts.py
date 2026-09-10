"""Cortex Stick facts channel — courier sync for the structured Facts store.

Memories and trajectories are files; facts are rows in {data_dir}/facts.sqlite,
so they can't ride the file-unit 3-way merge. Instead the stick carries a
canonical JSONL export (facts/facts.jsonl, encrypted like every other stick
file) and each host merges ROW-WISE under rules that mirror the FactsStore
promotion ladder:

  1. Same value on both sides → reassert: max confidence, newest timestamp.
  2. A NEWER confidence='false' row beats anything — demote() is an explicit
     judgment ("this is wrong") and must propagate; a STALE 'false' loses to
     a newer re-establishment.
  3. Otherwise the higher confidence rank wins (verified survives a newer
     high_probability challenge — the ladder's core promise), equal rank →
     newer last_updated, then value as a deterministic tie-break.

These rules are a total order per (entity, attribute), so the merge is
commutative and idempotent — no per-host base inventory is needed; the stick
simply holds the latest merged set. Facts are never deleted (demotion to
'false' IS the tombstone state), so there is no delete propagation and no
mass-delete guard to worry about.

Every change the courier applies to a host writes a fact_history audit row
(changed_by='stick:<id>') — the audit log is the loser preservation.

History travels too (4.21.2, the S04 offline-carry finding: the receiving
host could answer "what is it" but not "what did it used to be"). The stick
carries facts/fact_history.jsonl, the UNION of every host's ORIGIN rows —
the ones written by save()/demote() — identified by a content hash, so the
merge is set-union: idempotent, commutative, append-only. The courier's own
audit rows (changed_by='stick:…') stay local by design: each host's courier
bookkeeping describes what happened THERE, and carrying it would make every
sync a change on the other side.

Clock-skew note: cross-machine last_updated comparisons assume NTP-sane
clocks. Within a confidence rank a skewed clock can pick the wrong winner
until the fact is re-asserted; it can never silently destroy the loser
(fact_history records it) and never beats a higher rank.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from agentb.facts_store import _CONFIDENCE_RANK, FactsStore
from agentb.fsutil import atomic_write_bytes

FACTS_REL = "facts/facts.jsonl"
HISTORY_REL = "facts/fact_history.jsonl"

_HISTORY_FIELDS = ("entity", "attribute", "old_value", "new_value",
                   "old_confidence", "new_confidence", "reason",
                   "changed_at", "changed_by")
_COURIER_PREFIX = "stick:"

_FIELDS = ("entity", "attribute", "value", "confidence", "evidence_source",
           "source_memory_id", "source_agent", "created_at", "last_updated")


def _canon(row: dict) -> dict:
    return {k: row.get(k) for k in _FIELDS}


def merge_row(a: Optional[dict], b: Optional[dict]) -> dict:
    """Deterministic winner of two versions of one (entity, attribute) fact.

    Total order (see module docstring). The demotion rule comes FIRST —
    demote() flips confidence to 'false' but KEEPS the value, so a demoted
    row usually has the SAME value as the other host's live copy; a
    value-first comparison would resurrect it via the reassert branch."""
    if a is None or b is None:
        return _canon(a or b)  # type: ignore[arg-type]

    a_false, b_false = a["confidence"] == "false", b["confidence"] == "false"
    if a_false != b_false:                     # exactly one side demoted
        false_side, other = (a, b) if a_false else (b, a)
        if false_side["last_updated"] >= other["last_updated"]:
            return _canon(false_side)          # demotion is the latest word
        return _canon(other)                   # fact re-established later

    if a["value"] == b["value"]:               # reassert (both live/both false)
        if a["last_updated"] != b["last_updated"]:
            newer = a if a["last_updated"] > b["last_updated"] else b
        else:   # aux-field tie-break — first-arg-wins would break commutativity
            newer = max(a, b, key=lambda r: (r["evidence_source"] or "",
                                             r["source_agent"] or "",
                                             r["source_memory_id"] or ""))
        out = _canon(newer)
        out["confidence"] = max(a["confidence"], b["confidence"],
                                key=lambda c: _CONFIDENCE_RANK[c])
        out["created_at"] = min(a["created_at"], b["created_at"])
        return out

    ra, rb = _CONFIDENCE_RANK[a["confidence"]], _CONFIDENCE_RANK[b["confidence"]]
    if ra != rb:
        return _canon(a if ra > rb else b)     # ladder: higher rank survives
    if a["last_updated"] != b["last_updated"]:
        return _canon(a if a["last_updated"] > b["last_updated"] else b)
    return _canon(max(a, b, key=lambda r: r["value"]))   # deterministic tie


def _key(row: dict) -> tuple[str, str]:
    return (row["entity"], row["attribute"])


def dump_facts(db_path: Path) -> list[dict]:
    """All fact rows (including 'false' ones — they carry demotions)."""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM facts ORDER BY entity, attribute").fetchall()
    finally:
        conn.close()
    return [_canon(dict(r)) for r in rows]


def _encode_jsonl(rows: list[dict]) -> bytes:
    lines = [json.dumps(r, sort_keys=True) for r in
             sorted(rows, key=_key)]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def load_stick_facts(stick: Path, codec) -> list[dict]:
    p = stick / FACTS_REL
    if not p.is_file():
        return []
    data = codec.decode(p.read_bytes())
    return [json.loads(line) for line in data.decode("utf-8").splitlines()
            if line.strip()]


def apply_to_host(db_path: Path, winners: list[dict],
                  host_rows: dict[tuple[str, str], dict],
                  stick_id: str) -> int:
    """Write courier-won rows into the host DB, preserving their fields
    (save() would re-stamp timestamps and re-run the ladder, rejecting
    legitimate demotions). One transaction; a history row per change.

    The live server writes this DB 24/7. Winners were computed from a
    snapshot, so inside the write lock every key is re-read and re-merged
    against the LIVE row — a fact the server saved mid-merge must win its
    merge, never be clobbered by a stale snapshot (lost update + ladder
    violation). A key the live row wins is skipped; the stick copy catches
    up on the next sync (the merge is idempotent)."""
    changes = [w for w in winners
               if host_rows.get(_key(w)) != w]
    if not changes:
        return 0
    store = FactsStore(db_path)          # ensures schema exists (new host)
    conn = store._connect()
    applied = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        for w in changes:
            live_row = conn.execute(
                "SELECT * FROM facts WHERE entity=? AND attribute=?",
                _key(w)).fetchone()
            old = _canon(dict(live_row)) if live_row else None
            if old != host_rows.get(_key(w)):    # server wrote mid-merge
                w = merge_row(old, w)
                if w == old:
                    continue                     # live row wins — hands off
            conn.execute(
                "INSERT INTO facts (entity, attribute, value, confidence, "
                "evidence_source, source_memory_id, source_agent, created_at, "
                "last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity, attribute) DO UPDATE SET "
                "value=excluded.value, confidence=excluded.confidence, "
                "evidence_source=excluded.evidence_source, "
                "source_memory_id=excluded.source_memory_id, "
                "source_agent=excluded.source_agent, "
                "created_at=excluded.created_at, "
                "last_updated=excluded.last_updated",
                tuple(w[f] for f in _FIELDS),
            )
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, "
                "new_value, old_confidence, new_confidence, reason, "
                "changed_at, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (w["entity"], w["attribute"],
                 old["value"] if old else None, w["value"],
                 old["confidence"] if old else None, w["confidence"],
                 "cortex stick courier merge", now, f"stick:{stick_id}"),
            )
            applied += 1
        conn.commit()
    finally:
        conn.close()
    return applied


def sync_facts(db_path: Path, stick: Path, codec, stick_id: str,
               manifest_files: dict, *, dry_run: bool = False
               ) -> tuple[int, int, str, int]:
    """Merge host facts.sqlite ↔ stick facts JSONL. Returns
    (applied_to_host, sent_to_stick, canonical_sha_or_empty, payload_bytes).

    Skips silently only when NEITHER side has facts (nothing to courier).
    The stick file is manifest-covered like every other truth file; callers
    update manifest_files with the returned sha and size the free-space
    preflight with payload_bytes."""
    host_list = dump_facts(db_path)
    stick_list = load_stick_facts(stick, codec)
    if not host_list and not stick_list:
        return (0, 0, "", 0)

    host_rows = {_key(r): r for r in host_list}
    stick_rows = {_key(r): r for r in stick_list}
    winners = [merge_row(host_rows.get(k), stick_rows.get(k))
               for k in sorted(set(host_rows) | set(stick_rows))]

    to_host = sum(1 for w in winners if host_rows.get(_key(w)) != w)
    to_stick = sum(1 for w in winners if stick_rows.get(_key(w)) != w)

    payload = codec.encode(_encode_jsonl(winners))
    sha = hashlib.sha256(payload).hexdigest()
    if dry_run:
        return (to_host, to_stick, sha, len(payload))

    prev = manifest_files.get(FACTS_REL, {})
    if not to_host and not to_stick and prev.get("sha256") == sha:
        return (0, 0, sha, len(payload))       # settled — no writes, no churn

    applied = apply_to_host(db_path, winners, host_rows, stick_id)
    if to_stick or prev.get("sha256") != sha:
        # First-ever facts write has no prior manifest entry, so a crash
        # right here is invisible to the torn check — it self-heals on the
        # next sync (idempotent re-merge) rather than tearing.
        p = stick / FACTS_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(p, payload)
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise RuntimeError(f"Readback verify FAILED writing {p}")
        manifest_files[FACTS_REL] = {
            "sha256": sha,
            "version": prev.get("version", 0) + 1,
        }
    elif not prev:      # unreachable in practice; keep the entry consistent
        manifest_files[FACTS_REL] = {"sha256": sha, "version": 1}
    # import-only with matching stick bytes: leave the entry untouched
    return (applied, to_stick, sha, len(payload))


# ── fact_history channel (4.21.2) ────────────────────────────────────────────

def _history_canon(row: dict) -> dict:
    return {k: row.get(k) for k in _HISTORY_FIELDS}


def history_row_id(row: dict) -> str:
    """Content identity of one audit row — the autoincrement id is per-host
    and meaningless across machines."""
    return hashlib.sha256(
        json.dumps(_history_canon(row), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _is_origin_row(row: dict) -> bool:
    return not str(row.get("changed_by") or "").startswith(_COURIER_PREFIX)


def dump_history(db_path: Path) -> list[dict]:
    """Origin audit rows only (save/demote); courier rows stay local."""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        # Probe by name: a facts.sqlite that predates the table has no
        # history. Every OTHER error (locked, I/O) must propagate — an
        # empty answer here would make the caller re-import the whole
        # stick log as duplicates (reviewer finding, 4.21.2).
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='fact_history'").fetchone():
            return []
        rows = conn.execute(
            "SELECT * FROM fact_history ORDER BY changed_at, id").fetchall()
    finally:
        conn.close()
    return [_history_canon(dict(r)) for r in rows if _is_origin_row(dict(r))]


def _encode_history_jsonl(rows: dict[str, dict]) -> bytes:
    lines = [json.dumps(rows[k], sort_keys=True) for k in sorted(rows)]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _decode_history(raw: bytes, codec) -> list[dict]:
    if not raw:
        return []
    data = codec.decode(raw)
    return [json.loads(line) for line in data.decode("utf-8").splitlines()
            if line.strip()]


def load_stick_history(stick: Path, codec) -> list[dict]:
    p = stick / HISTORY_REL
    return _decode_history(p.read_bytes() if p.is_file() else b"", codec)


def sync_fact_history(db_path: Path, stick: Path, codec,
                      manifest_files: dict, *, dry_run: bool = False
                      ) -> tuple[int, int, str, int]:
    """Set-union merge of origin audit rows host ↔ stick. Returns
    (imported_to_host, sent_to_stick, canonical_sha_or_empty, payload_bytes)
    — the same shape as sync_facts, and manifest-covered the same way.

    Rows are never rewritten or removed; a row the host lacks is INSERTed
    with a fresh local id. Skips silently when neither side has any."""
    host_rows = {history_row_id(r): r for r in dump_history(db_path)}
    stick_path = stick / HISTORY_REL
    stick_bytes = stick_path.read_bytes() if stick_path.is_file() else b""
    stick_rows = {history_row_id(r): _history_canon(r)
                  for r in _decode_history(stick_bytes, codec)}
    if not host_rows and not stick_rows:
        return (0, 0, "", 0)

    union = {**stick_rows, **host_rows}
    to_host = [union[k] for k in sorted(set(stick_rows) - set(host_rows))]
    to_stick = len(set(host_rows) - set(stick_rows))

    prev = manifest_files.get(HISTORY_REL, {})
    if not to_host and not to_stick and stick_bytes:
        # Settled: the union IS the stick file, so its bytes are the payload
        # — no re-encrypt of the whole log just to learn nothing changed.
        sha = hashlib.sha256(stick_bytes).hexdigest()
        if dry_run or prev.get("sha256") == sha:
            return (0, 0, sha, len(stick_bytes))
    payload = codec.encode(_encode_history_jsonl(union))
    sha = hashlib.sha256(payload).hexdigest()
    if dry_run:
        return (len(to_host), to_stick, sha, len(payload))
    if not to_host and not to_stick and prev.get("sha256") == sha:
        return (0, 0, sha, len(payload))       # settled — no writes, no churn

    imported = 0
    if to_host:
        store = FactsStore(db_path)          # ensures schema exists (new host)
        conn = store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO fact_history (entity, attribute, old_value, "
                "new_value, old_confidence, new_confidence, reason, "
                "changed_at, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(r[f] for f in _HISTORY_FIELDS) for r in to_host],
            )
            conn.commit()
            imported = len(to_host)
        finally:
            conn.close()

    if to_stick or prev.get("sha256") != sha:
        p = stick / HISTORY_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(p, payload)
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise RuntimeError(f"Readback verify FAILED writing {p}")
        manifest_files[HISTORY_REL] = {
            "sha256": sha,
            "version": prev.get("version", 0) + 1,
        }
    return (imported, to_stick, sha, len(payload))
