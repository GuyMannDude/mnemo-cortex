"""Mnemo v4 Phase 3: structured Facts store with confidence + evidence.

Bundles Addition 2 (Structured Facts Table) + Addition 5 (Confidence + Evidence
Fields) from brain/mnemo-v4-phase3-facts-confidence-spec.md.

Storage: ~/.agentb/facts.sqlite — shared global, WAL mode.
- facts: composite PK (entity, attribute). One current value per pair.
- fact_history: append-only audit log of every change.

Confidence ladder: false < high_probability < verified.
Promotion: verified can only be overwritten by verified (with audit). Lower
confidence cannot silently overwrite higher.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

from agentb.provenance import VALID_CATEGORIES
from agentb.redact import redact_text
from agentb.vec import prompt_words   # one tokeniser, both lanes (4.24.0)
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CONFIDENCE_LEVELS = ("false", "high_probability", "verified")
_CONFIDENCE_RANK = {c: i for i, c in enumerate(CONFIDENCE_LEVELS)}

# v4.23 authority tiers — a tier says WHO may change a slot, not what it is
# about. `open` keeps the confidence ladder as the only rule. A locked slot
# accepts a write only from evidence of its own kind: a machine for `probe`,
# Guy's word for `declared`. Anything else is recorded as a proposal — never
# as the value, and never as an error. (Specimen: an agent's loose "Tailscale
# is the only path" sentence auto-captured and served as topology, 2026-09-15.)
AUTHORITY_LEVELS = ("open", "probe", "declared")
_AUTHORITY_EVIDENCE = {
    "open": (),
    "probe": ("probe:", "tool:"),
    "declared": ("statement:guy",),
}
# Locking or unlocking a slot is itself a declared act.
_AUTHORITY_CHANGE_EVIDENCE = ("statement:guy",)


# 4.26.0 proposal envelope. A memory-kind proposal keys on a sentinel so the
# NOT NULL entity/attribute columns stay honest: entity='memory:<tenant>/<id>'
# (facts.sqlite is global, memory ids are per tenant), attribute='category'
# or 'note'. Nothing but a proposal row or its fact_history row ever carries
# the sentinel — the facts table never does.
MEMORY_KINDS = {"memory_category": "category", "memory_note": "note"}
PROPOSAL_KINDS = ("fact",) + tuple(MEMORY_KINDS)
MEMORY_ENTITY_PREFIX = "memory:"
MAX_QUOTE_CHARS = 500
# A rejected memory-kind proposal holds identical re-proposals this long, so
# a nightly machine opinion is not refiled every night after Guy said no.
REJECTED_HOLD_SECONDS = 30 * 86400


def memory_target_ref(tenant: str, memory_id: str) -> str:
    return f"{MEMORY_ENTITY_PREFIX}{tenant}/{memory_id}"


def parse_memory_target_ref(ref: str) -> tuple[str, str]:
    """'memory:<tenant>/<id>' -> (tenant, id). Raises ValueError."""
    if not ref.startswith(MEMORY_ENTITY_PREFIX) or "/" not in ref:
        raise ValueError(f"not a memory target: {ref!r}")
    tenant, _, memory_id = ref[len(MEMORY_ENTITY_PREFIX):].partition("/")
    if not tenant or not memory_id:
        raise ValueError(f"not a memory target: {ref!r}")
    return tenant, memory_id


def evidence_allowed(authority: str, evidence_source: str) -> bool:
    """May evidence of this kind write a slot of this tier?"""
    if authority == "open":
        return True
    es = (evidence_source or "").strip().lower()
    return any(es.startswith(p) for p in _AUTHORITY_EVIDENCE.get(authority, ()))


@dataclass
class Fact:
    entity: str
    attribute: str
    value: str
    confidence: str
    evidence_source: str
    source_memory_id: Optional[str]
    source_agent: Optional[str]
    created_at: float
    last_updated: float
    # v4.23 authority tiers (defaults keep pre-4.23 rows and callers whole)
    authority: str = "open"
    probe_cmd: Optional[str] = None
    probe_host: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FactWriteResult:
    written: bool
    was_contradiction: bool
    previous_value: Optional[str] = None
    previous_confidence: Optional[str] = None
    reason: str = ""
    # v4.23: set when a write to a locked slot was recorded as a proposal
    proposal_id: Optional[int] = None
    authority: str = "open"


class FactsStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS facts (
        entity           TEXT NOT NULL,
        attribute        TEXT NOT NULL,
        value            TEXT NOT NULL,
        confidence       TEXT NOT NULL CHECK(confidence IN ('verified', 'high_probability', 'false')),
        evidence_source  TEXT NOT NULL,
        source_memory_id TEXT,
        source_agent     TEXT,
        created_at       REAL NOT NULL,
        last_updated     REAL NOT NULL,
        PRIMARY KEY (entity, attribute)
    );
    CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity);
    CREATE INDEX IF NOT EXISTS idx_facts_confidence ON facts(confidence);
    CREATE INDEX IF NOT EXISTS idx_facts_last_updated ON facts(last_updated);

    CREATE TABLE IF NOT EXISTS fact_history (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        entity           TEXT NOT NULL,
        attribute        TEXT NOT NULL,
        old_value        TEXT,
        new_value        TEXT,
        old_confidence   TEXT,
        new_confidence   TEXT,
        reason           TEXT NOT NULL,
        changed_at       REAL NOT NULL,
        changed_by       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_history_entity_attr ON fact_history(entity, attribute);
    CREATE INDEX IF NOT EXISTS idx_history_changed_at ON fact_history(changed_at);

    CREATE TABLE IF NOT EXISTS fact_proposals (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        entity            TEXT NOT NULL,
        attribute         TEXT NOT NULL,
        proposed_value    TEXT NOT NULL,
        confidence        TEXT NOT NULL,
        evidence_source   TEXT NOT NULL,
        source_agent      TEXT,
        authority         TEXT NOT NULL,
        current_value     TEXT,
        status            TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
        seen_count        INTEGER NOT NULL DEFAULT 1,
        created_at        REAL NOT NULL,
        last_seen         REAL,
        resolved_at       REAL,
        resolved_by       TEXT,
        resolution_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_proposals_status ON fact_proposals(status);

    -- 4.26.0: the Jev live-path shadow's counters. Counters only — no rows
    -- per memory, no text. bucket is the 0.1-wide Jev confidence band for
    -- ok/agree/disagree and '-' for counts taken before Jev answered.
    CREATE TABLE IF NOT EXISTS jev_shadow_stats (
        day             TEXT NOT NULL,
        tenant          TEXT NOT NULL,
        stored_category TEXT NOT NULL,
        bucket          TEXT NOT NULL,
        eligible        INTEGER NOT NULL DEFAULT 0,
        dropped         INTEGER NOT NULL DEFAULT 0,
        attempted       INTEGER NOT NULL DEFAULT 0,
        ok              INTEGER NOT NULL DEFAULT 0,
        timeout         INTEGER NOT NULL DEFAULT 0,
        error           INTEGER NOT NULL DEFAULT 0,
        agree           INTEGER NOT NULL DEFAULT 0,
        disagree        INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, tenant, stored_category, bucket)
    );
    """

    # v4.23: stores older than the authority columns get them on first open.
    # ALTER is idempotent by inspection; two first-touch connections racing
    # the same ALTER raise OperationalError, which the retry in _connect
    # re-inspects and finds already done.
    _NEW_COLUMNS = {
        "facts": (
            ("authority", "TEXT NOT NULL DEFAULT 'open'"),
            ("probe_cmd", "TEXT"),
            ("probe_host", "TEXT"),
        ),
        # 4.23.0 pre-deploy review #4: dedupe columns. A 4.23.0-shaped table
        # without them would 500 every held write (review round 2, #1).
        "fact_proposals": (
            ("seen_count", "INTEGER NOT NULL DEFAULT 1"),
            ("last_seen", "REAL"),
            # 4.26.0 proposal envelope: one table for every machine proposal.
            # Existing rows read as target_kind='fact' with no quotes.
            ("target_kind", "TEXT NOT NULL DEFAULT 'fact'"),
            ("target_ref", "TEXT"),
            ("evidence_quotes", "TEXT NOT NULL DEFAULT '[]'"),
            ("confidence_score", "REAL"),
            ("source_run", "TEXT"),
        ),
    }

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        for table, columns in self._NEW_COLUMNS.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # 4.26.0: the index names envelope columns, so it can only be built
        # after the ALTERs — never in SCHEMA, which runs first on old stores.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_target "
                     "ON fact_proposals(target_kind, target_ref, status)")

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # Re-init schema on every connect. CREATE TABLE IF NOT EXISTS is
        # idempotent + cheap; this protects against the file being deleted
        # out from under the server (operator error, disk issue, etc) without
        # requiring a service restart. Caught during phase3 deploy when a
        # test-data cleanup deleted facts.sqlite and every subsequent POST
        # 500'd with "no such table: facts" until restart.
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Two connections racing this block on a brand-new DB (concurrent
        # first touch) can throw a transient "database is locked" from the
        # WAL switch / schema DDL. Retry briefly — the loser's work is
        # idempotent anyway (IF NOT EXISTS). Seen as the flaky CI hang in
        # test_concurrent_first_saves_never_raise (2026-07-12).
        for attempt in range(3):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(self.SCHEMA)
                self._migrate_columns(conn)
                return conn
            except sqlite3.OperationalError:
                if attempt == 2:
                    conn.close()
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")

    def _init_schema(self) -> None:
        # Kept for explicit init call (and to surface schema errors at startup,
        # not at first request). _connect() also re-runs it as a safety net.
        conn = self._connect()
        try:
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _normalize_entity(entity: str) -> str:
        return entity.strip().lower()

    @staticmethod
    def _normalize_attribute(attribute: str) -> str:
        return attribute.strip().lower().replace(" ", "_").replace("-", "_")

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> Fact:
        keys = row.keys()
        return Fact(
            entity=row["entity"],
            attribute=row["attribute"],
            value=row["value"],
            confidence=row["confidence"],
            evidence_source=row["evidence_source"],
            source_memory_id=row["source_memory_id"],
            source_agent=row["source_agent"],
            created_at=row["created_at"],
            last_updated=row["last_updated"],
            authority=(row["authority"] if "authority" in keys else None) or "open",
            probe_cmd=row["probe_cmd"] if "probe_cmd" in keys else None,
            probe_host=row["probe_host"] if "probe_host" in keys else None,
        )

    def get(self, entity: str, attribute: str, include_false: bool = False) -> Optional[Fact]:
        e = self._normalize_entity(entity)
        a = self._normalize_attribute(attribute)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM facts WHERE entity=? AND attribute=?", (e, a)
            ).fetchone()
            if not row:
                return None
            if row["confidence"] == "false" and not include_false:
                return None
            return self._row_to_fact(row)
        finally:
            conn.close()

    def query(
        self,
        entity: Optional[str] = None,
        attribute: Optional[str] = None,
        value_contains: Optional[str] = None,
        confidence: Optional[str] = None,
        changed_since: Optional[float] = None,
        limit: int = 20,
    ) -> list[Fact]:
        if confidence is not None and confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        limit = max(1, min(int(limit), 100))

        clauses: list[str] = []
        params: list = []
        if entity is not None:
            clauses.append("entity = ?")
            params.append(self._normalize_entity(entity))
        if attribute is not None:
            clauses.append("attribute = ?")
            params.append(self._normalize_attribute(attribute))
        if value_contains is not None:
            clauses.append("value LIKE ?")
            params.append(f"%{value_contains}%")
        if confidence is not None:
            clauses.append("confidence = ?")
            params.append(confidence)
        if changed_since is not None:
            clauses.append("last_updated >= ?")
            params.append(float(changed_since))

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM facts {where} ORDER BY last_updated DESC LIMIT ?"
        params.append(limit)

        conn = self._connect()
        try:
            return [self._row_to_fact(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _propose(self, conn: sqlite3.Connection, existing: sqlite3.Row, e: str, a: str,
                 value: str, confidence: str, evidence_source: str,
                 source_agent: Optional[str], authority: str, now: float) -> tuple[int, int]:
        """Record a write the lock held. Returns (proposal_id, seen_count).

        Identical pending proposals collapse onto one row with a bumped
        seen_count (the dreamer re-extracts the same sentence nightly; thirty
        rows a month would bury the brief's ten-row block). Only a NEW
        proposal gets a fact_history row — a repeat is not a new event."""
        # 4.26.0 envelope: the evidence string is the row's one quote
        quotes = json.dumps([redact_text(evidence_source)[0][:MAX_QUOTE_CHARS]])
        dup = conn.execute(
            "SELECT id, seen_count FROM fact_proposals WHERE target_kind='fact' "
            "AND entity=? AND attribute=? "
            "AND proposed_value=? AND confidence=? AND status='pending'",
            (e, a, value, confidence),
        ).fetchone()
        if dup is not None:
            seen = int(dup["seen_count"]) + 1
            # the row names the MOST RECENT asker and evidence; the count says
            # how many there were (review round 2, #4)
            conn.execute(
                "UPDATE fact_proposals SET seen_count=?, last_seen=?, current_value=?, "
                "evidence_source=?, source_agent=?, evidence_quotes=?, source_run=? WHERE id=?",
                (seen, now, existing["value"], evidence_source, source_agent, quotes,
                 source_agent, dup["id"]),
            )
            return int(dup["id"]), seen
        cur = conn.execute(
            "INSERT INTO fact_proposals (entity, attribute, proposed_value, confidence, "
            "evidence_source, source_agent, authority, current_value, created_at, last_seen, "
            "target_kind, evidence_quotes, source_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fact', ?, ?)",
            (e, a, value, confidence, evidence_source, source_agent, authority, existing["value"],
             now, now, quotes, source_agent),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
            "old_confidence, new_confidence, reason, changed_at, changed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e, a, existing["value"], value, existing["confidence"], confidence,
             f"proposal #{pid} — locked:{authority}, this evidence may not write the slot",
             now, source_agent),
        )
        return pid, 1

    def save(
        self,
        entity: str,
        attribute: str,
        value: str,
        confidence: str,
        evidence_source: str,
        source_memory_id: Optional[str] = None,
        source_agent: Optional[str] = None,
    ) -> FactWriteResult:
        """UPSERT a fact, enforcing the promotion ladder + audit history.

        Returns FactWriteResult describing what happened. Never raises on
        legitimate-but-rejected writes (lower-confidence vs verified existing);
        only raises on invalid inputs.
        """
        if confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        if not evidence_source.strip():
            raise ValueError("evidence_source is required")
        e = self._normalize_entity(entity)
        a = self._normalize_attribute(attribute)
        now = time.time()

        conn = self._connect()
        try:
            # BEGIN IMMEDIATE takes the write lock up front so the
            # read-check-write below is atomic across processes (the server,
            # the dreamer, and the CLI all open this DB). Without it two
            # writers could both read "no existing fact" and race the INSERT
            # (uncaught IntegrityError → 500), or a stale high_probability
            # value could overwrite a fact verified in between.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM facts WHERE entity=? AND attribute=?", (e, a)
            ).fetchone()

            if existing is None:
                conn.execute(
                    "INSERT INTO facts (entity, attribute, value, confidence, evidence_source, "
                    "source_memory_id, source_agent, created_at, last_updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e, a, value, confidence, evidence_source, source_memory_id, source_agent, now, now),
                )
                conn.execute(
                    "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                    "old_confidence, new_confidence, reason, changed_at, changed_by) "
                    "VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?)",
                    (e, a, value, confidence, "initial assertion", now, source_agent),
                )
                conn.commit()
                return FactWriteResult(written=True, was_contradiction=False, reason="initial")

            # v4.23 authority tiers: a locked slot answers only to its own
            # kind of evidence. Everything else becomes a proposal — recorded,
            # never written, never an error (a proposal is the system working).
            authority = existing["authority"] or "open"
            if not evidence_allowed(authority, evidence_source):
                if existing["value"] == value:
                    conn.rollback()
                    return FactWriteResult(
                        written=False, was_contradiction=False,
                        previous_value=existing["value"], previous_confidence=existing["confidence"],
                        reason=f"locked:{authority} — value unchanged, nothing proposed",
                        authority=authority,
                    )
                pid, seen = self._propose(conn, existing, e, a, value, confidence,
                                          evidence_source, source_agent, authority, now)
                conn.commit()
                return FactWriteResult(
                    written=False, was_contradiction=True,
                    previous_value=existing["value"], previous_confidence=existing["confidence"],
                    reason=(f"locked:{authority} — recorded as proposal #{pid}" if seen == 1
                            else f"locked:{authority} — already proposed as #{pid} (seen {seen}x)"),
                    proposal_id=pid, authority=authority,
                )

            if existing["value"] == value:
                new_conf = confidence if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[existing["confidence"]] else existing["confidence"]
                conn.execute(
                    "UPDATE facts SET evidence_source=?, last_updated=?, confidence=? "
                    "WHERE entity=? AND attribute=?",
                    (evidence_source, now, new_conf, e, a),
                )
                conn.execute(
                    "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                    "old_confidence, new_confidence, reason, changed_at, changed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e, a, existing["value"], value, existing["confidence"], new_conf, "reasserted", now, source_agent),
                )
                conn.commit()
                return FactWriteResult(
                    written=True, was_contradiction=False,
                    previous_value=existing["value"], previous_confidence=existing["confidence"],
                    reason="reasserted", authority=authority,
                )

            # Different value → contradiction. Apply promotion ladder.
            new_rank = _CONFIDENCE_RANK[confidence]
            existing_rank = _CONFIDENCE_RANK[existing["confidence"]]

            if new_rank >= existing_rank:
                conn.execute(
                    "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                    "old_confidence, new_confidence, reason, changed_at, changed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e, a, existing["value"], value, existing["confidence"], confidence,
                     "contradicted by new evidence", now, source_agent),
                )
                conn.execute(
                    "UPDATE facts SET value=?, confidence=?, evidence_source=?, "
                    "source_memory_id=?, source_agent=?, last_updated=? "
                    "WHERE entity=? AND attribute=?",
                    (value, confidence, evidence_source, source_memory_id, source_agent, now, e, a),
                )
                conn.commit()
                return FactWriteResult(
                    written=True, was_contradiction=True,
                    previous_value=existing["value"], previous_confidence=existing["confidence"],
                    reason="overwritten by equal-or-higher confidence", authority=authority,
                )

            # Lower confidence vs higher existing — REJECT but log
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                "old_confidence, new_confidence, reason, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (e, a, existing["value"], value, existing["confidence"], confidence,
                 "rejected — existing higher confidence takes precedence", now, source_agent),
            )
            conn.commit()
            return FactWriteResult(
                written=False, was_contradiction=True,
                previous_value=existing["value"], previous_confidence=existing["confidence"],
                reason="rejected — existing higher confidence takes precedence", authority=authority,
            )
        finally:
            conn.close()

    def demote(self, entity: str, attribute: str, reason: str, changed_by: Optional[str] = None,
               evidence_source: Optional[str] = None) -> FactWriteResult:
        """Force a fact to confidence='false' without supplying a new value.

        Used when something is known wrong but the correct value isn't known yet.
        Required because the promotion ladder otherwise blocks verified→false
        transitions.

        v4.23: a demote is a write. On a locked slot it answers to the same
        evidence as any other write — a probe slot needs `probe:`/`tool:`
        evidence (the machine said the value is gone), a declared slot needs
        Guy's word. Anything else is recorded as a proposal to demote
        (confidence 'false'), never applied. Without this the demote tool was
        the one door around every lock (review finding #1, 2026-09-15).
        """
        if not reason.strip():
            raise ValueError("reason is required for demote")
        e = self._normalize_entity(entity)
        a = self._normalize_attribute(attribute)
        now = time.time()

        conn = self._connect()
        try:
            # Same cross-process atomicity as save() — see the comment there.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM facts WHERE entity=? AND attribute=?", (e, a)
            ).fetchone()
            if existing is None:
                return FactWriteResult(written=False, was_contradiction=False, reason="no such fact")
            authority = existing["authority"] or "open"
            if existing["confidence"] == "false":
                return FactWriteResult(
                    written=False, was_contradiction=False,
                    previous_confidence="false", reason="already false", authority=authority,
                )

            if not evidence_allowed(authority, evidence_source or ""):
                # the proposal carries the reason whatever the evidence — Guy
                # resolves it reading this row, not the history table
                # 4.24.0: no f-string nested inside an f-string with the same
                # quotes — legal on 3.12+ (PEP 701), a SyntaxError on 3.11 (CI red x2)
                who = (evidence_source or "").strip() or f"agent:{changed_by or 'unknown'}"
                es = f"{who} — demote: {reason.strip()}"
                pid, seen = self._propose(conn, existing, e, a, existing["value"], "false",
                                          es[:200], changed_by, authority, now)
                conn.commit()
                return FactWriteResult(
                    written=False, was_contradiction=True,
                    previous_value=existing["value"], previous_confidence=existing["confidence"],
                    reason=(f"locked:{authority} — demote recorded as proposal #{pid}" if seen == 1
                            else f"locked:{authority} — demote already proposed as #{pid} (seen {seen}x)"),
                    proposal_id=pid, authority=authority,
                )

            conn.execute(
                "UPDATE facts SET confidence='false', evidence_source=?, last_updated=? "
                "WHERE entity=? AND attribute=?",
                (f"demoted: {reason}", now, e, a),
            )
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                "old_confidence, new_confidence, reason, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?, 'false', ?, ?, ?)",
                (e, a, existing["value"], existing["value"], existing["confidence"],
                 f"demote: {reason}", now, changed_by),
            )
            conn.commit()
            return FactWriteResult(
                written=True, was_contradiction=False,
                previous_value=existing["value"], previous_confidence=existing["confidence"],
                reason="demoted", authority=authority,
            )
        finally:
            conn.close()

    def history(self, entity: str, attribute: str, limit: int = 50) -> list[dict]:
        e = self._normalize_entity(entity)
        a = self._normalize_attribute(attribute)
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM fact_history WHERE entity=? AND attribute=? "
                "ORDER BY changed_at DESC LIMIT ?",
                (e, a, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def contradictions(self, since: Optional[float] = None, limit: int = 100) -> list[dict]:
        """Recent rejected-by-promotion-ladder writes + confidence='false' rows.

        The debug view for catching extraction drift.
        """
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            params: list = []
            since_clause = ""
            if since is not None:
                since_clause = " AND changed_at >= ?"
                params.append(float(since))
            params.append(limit)
            rows = conn.execute(
                "SELECT * FROM fact_history WHERE "
                "(reason LIKE 'rejected%' OR reason LIKE 'contradicted%' OR reason LIKE 'demote%' "
                "OR reason LIKE 'proposal%')"
                + since_clause +
                " ORDER BY changed_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── v4.23 authority tiers ──────────────────────────────────────────────

    def set_authority(
        self,
        entity: str,
        attribute: str,
        authority: str,
        evidence_source: str,
        changed_by: Optional[str] = None,
        probe_cmd: Optional[str] = None,
        probe_host: Optional[str] = None,
    ) -> FactWriteResult:
        """Set a slot's tier. Locking or unlocking is itself a declared act:
        only Guy's word (`statement:guy…` evidence) may do it. A probe slot
        must carry the command that re-asks it — a probe fact you cannot
        re-ask is a declared fact wearing a lab coat."""
        if authority not in AUTHORITY_LEVELS:
            raise ValueError(f"authority must be one of {AUTHORITY_LEVELS}")
        es = (evidence_source or "").strip().lower()
        if not any(es.startswith(p) for p in _AUTHORITY_CHANGE_EVIDENCE):
            return FactWriteResult(
                written=False, was_contradiction=False,
                reason="rejected — an authority change needs statement:guy evidence",
            )
        if authority == "probe" and not (probe_cmd or "").strip():
            return FactWriteResult(
                written=False, was_contradiction=False,
                reason="rejected — a probe slot needs probe_cmd (how to re-ask it)",
            )
        e = self._normalize_entity(entity)
        a = self._normalize_attribute(attribute)
        now = time.time()
        cmd = probe_cmd if authority == "probe" else None
        host = probe_host if authority == "probe" else None

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM facts WHERE entity=? AND attribute=?", (e, a)
            ).fetchone()
            if existing is None:
                conn.rollback()
                return FactWriteResult(written=False, was_contradiction=False, reason="no such fact")
            old = existing["authority"] or "open"
            conn.execute(
                "UPDATE facts SET authority=?, probe_cmd=?, probe_host=?, last_updated=? "
                "WHERE entity=? AND attribute=?",
                (authority, cmd, host, now, e, a),
            )
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                "old_confidence, new_confidence, reason, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (e, a, existing["value"], existing["value"], existing["confidence"], existing["confidence"],
                 f"authority: {old} -> {authority} ({evidence_source.strip()[:120]})", now, changed_by),
            )
            conn.commit()
            return FactWriteResult(
                written=True, was_contradiction=False,
                previous_value=existing["value"], previous_confidence=existing["confidence"],
                reason=f"authority: {old} -> {authority}", authority=authority,
            )
        finally:
            conn.close()

    def proposals(self, status: Optional[str] = "pending", limit: int = 50,
                  kind: Optional[str] = None) -> list[dict]:
        """Proposals, newest first. status=None → all; kind=None → every
        kind (4.26.0). evidence_quotes comes back decoded, as a list."""
        limit = max(1, min(int(limit), 500))
        if kind is not None and kind not in PROPOSAL_KINDS:
            raise ValueError(f"kind must be one of {PROPOSAL_KINDS}")
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if kind:
            clauses.append("target_kind=?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM fact_proposals {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence_quotes"] = json.loads(d.get("evidence_quotes") or "[]")
            except ValueError:
                d["evidence_quotes"] = []
            out.append(d)
        return out

    def proposal(self, proposal_id: int) -> Optional[dict]:
        """One proposal row by id, or None."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM fact_proposals WHERE id=?",
                               (int(proposal_id),)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def pending_counts(self) -> dict[str, int]:
        """{kind: pending count} for every kind, zeros included."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT target_kind, COUNT(*) AS n FROM fact_proposals "
                "WHERE status='pending' GROUP BY target_kind").fetchall()
        finally:
            conn.close()
        counts = {k: 0 for k in PROPOSAL_KINDS}
        counts.update({r["target_kind"]: int(r["n"]) for r in rows})
        return counts

    # ── 4.26.0 proposal envelope: machine opinions about a memory ─────────

    def propose_memory(
        self,
        target_kind: str,
        tenant: str,
        memory_id: str,
        memory_dir: Path,
        proposed_value: str,
        evidence_quotes: list,
        confidence_score: float,
        source_run: str,
        source_agent: Optional[str] = None,
        current_value: Optional[str] = None,
    ) -> tuple[int, int]:
        """Record a machine proposal about one memory. Never touches the
        memory. Returns (proposal_id, seen_count):

        - a new row → (id, 1)
        - an identical PENDING proposal → that row, seen_count bumped
        - an identical proposal REJECTED within 30 days → (rejected_id, 0):
          held by the rejection, nothing written

        Refuses (ValueError) an unknown kind, no quotes, a quote over 500
        chars, a confidence outside 0-1, a category that is not one, or a
        memory that is not on disk in `memory_dir` for this tenant. Quotes
        (and a note's value) pass through redact_text before storage."""
        if target_kind not in MEMORY_KINDS:
            raise ValueError(f"target_kind must be one of {tuple(MEMORY_KINDS)}")
        if not isinstance(evidence_quotes, list) or not evidence_quotes:
            raise ValueError("evidence_quotes needs at least one quote")
        for q in evidence_quotes:
            if not isinstance(q, str) or not q.strip():
                raise ValueError("every evidence quote must be non-empty text")
            if len(q) > MAX_QUOTE_CHARS:
                raise ValueError(f"evidence quote over {MAX_QUOTE_CHARS} chars")
        try:
            score = float(confidence_score)
        except (TypeError, ValueError):
            raise ValueError("confidence_score must be a number 0-1") from None
        if not 0.0 <= score <= 1.0:
            raise ValueError("confidence_score must be between 0 and 1")
        if not (source_run or "").strip():
            raise ValueError("source_run is required")
        value = (proposed_value or "").strip()
        if target_kind == "memory_category":
            if value not in VALID_CATEGORIES:
                raise ValueError(f"proposed category {value!r} is not a category")
        else:
            if not value:
                raise ValueError("proposed_value is required")
            value = redact_text(value)[0]
        if not (Path(memory_dir) / f"{memory_id}.json").is_file():
            raise ValueError(f"no memory {memory_id} for tenant {tenant}")

        ref = memory_target_ref(tenant, memory_id)
        attribute = MEMORY_KINDS[target_kind]
        quotes = json.dumps([redact_text(q)[0] for q in evidence_quotes])
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            dup = conn.execute(
                "SELECT id, status, seen_count FROM fact_proposals WHERE target_kind=? "
                "AND target_ref=? AND proposed_value=? AND (status='pending' OR "
                "(status='rejected' AND resolved_at >= ?)) "
                "ORDER BY status='pending' DESC, id DESC LIMIT 1",
                (target_kind, ref, value, now - REJECTED_HOLD_SECONDS),
            ).fetchone()
            if dup is not None and dup["status"] == "rejected":
                conn.rollback()
                return int(dup["id"]), 0
            if dup is not None:
                seen = int(dup["seen_count"]) + 1
                conn.execute(
                    "UPDATE fact_proposals SET seen_count=?, last_seen=?, current_value=?, "
                    "evidence_quotes=?, confidence_score=?, source_run=?, evidence_source=?, "
                    "source_agent=? WHERE id=?",
                    (seen, now, current_value, quotes, score, source_run, source_run,
                     source_agent, dup["id"]),
                )
                conn.commit()
                return int(dup["id"]), seen
            cur = conn.execute(
                "INSERT INTO fact_proposals (entity, attribute, proposed_value, confidence, "
                "evidence_source, source_agent, authority, current_value, created_at, last_seen, "
                "target_kind, target_ref, evidence_quotes, confidence_score, source_run) "
                "VALUES (?, ?, ?, 'n/a', ?, ?, 'n/a', ?, ?, ?, ?, ?, ?, ?, ?)",
                (ref, attribute, value, source_run, source_agent, current_value, now, now,
                 target_kind, ref, quotes, score, source_run),
            )
            pid = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                "old_confidence, new_confidence, reason, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                (ref, attribute, current_value, value,
                 f"{target_kind} proposal #{pid} by {source_run}", now, source_agent),
            )
            conn.commit()
            return pid, 1
        finally:
            conn.close()

    def resolve_proposal(self, proposal_id: int, action: str, by: str, reason: str = "",
                         apply_memory=None) -> FactWriteResult:
        """Accept or reject a proposal. Accepting is Guy's word relayed by
        `by`: the proposed value lands as verified with `statement:guy`
        evidence naming the proposal — the one route past a lock. Rejecting
        only closes it; the slot is untouched.

        4.26.0: a memory-kind proposal needs `apply_memory(tenant, memory_id,
        new_value, proposal_id) -> (old_value, undo)` from the caller (the
        server owns the memory files and the atomic writer). It runs inside
        this transaction: a failed apply raises and the proposal stays
        pending; if the proposal's own write fails after a successful apply,
        `undo()` puts the memory back before the error propagates. Only
        memory_category can be accepted yet."""
        if action not in ("accept", "reject"):
            raise ValueError("action must be 'accept' or 'reject'")
        if not (by or "").strip():
            raise ValueError("by is required")
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            p = conn.execute("SELECT * FROM fact_proposals WHERE id=?", (int(proposal_id),)).fetchone()
            if p is None:
                conn.rollback()
                return FactWriteResult(written=False, was_contradiction=False, reason="no such proposal")
            if p["status"] != "pending":
                conn.rollback()
                return FactWriteResult(written=False, was_contradiction=False, reason=f"already {p['status']}")
            if (p["target_kind"] or "fact") != "fact":
                return self._resolve_memory(conn, p, action, by, reason, now, apply_memory)
            e, a = p["entity"], p["attribute"]
            existing = conn.execute(
                "SELECT * FROM facts WHERE entity=? AND attribute=?", (e, a)
            ).fetchone()
            if existing is None:
                conn.rollback()
                return FactWriteResult(written=False, was_contradiction=False, reason="fact no longer exists")
            tier = existing["authority"] or "open"
            conn.execute(
                "UPDATE fact_proposals SET status=?, resolved_at=?, resolved_by=?, resolution_reason=? WHERE id=?",
                ("accepted" if action == "accept" else "rejected", now, by, reason, p["id"]),
            )
            if action == "reject":
                conn.execute(
                    "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                    "old_confidence, new_confidence, reason, changed_at, changed_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e, a, existing["value"], p["proposed_value"], existing["confidence"], existing["confidence"],
                     f"proposal #{p['id']} rejected by {by}: {reason}", now, by),
                )
                conn.commit()
                return FactWriteResult(
                    written=False, was_contradiction=False,
                    previous_value=existing["value"], previous_confidence=existing["confidence"],
                    reason=f"proposal #{p['id']} rejected", proposal_id=p["id"], authority=tier,
                )
            evidence = f"statement:guy accepted proposal #{p['id']} via {by} <- {p['evidence_source']}"
            # A demote proposal (confidence 'false') lands as false; every
            # other accepted proposal lands as verified — it is Guy's word now.
            # A demote proposal froze proposed_value at proposal time; its
            # intent is "mark this slot false", so it lands on whatever the
            # value is NOW — never reverting a correction made since.
            is_demote = p["confidence"] == "false"
            new_conf = "false" if is_demote else "verified"
            new_value = existing["value"] if is_demote else p["proposed_value"]
            conn.execute(
                "UPDATE facts SET value=?, confidence=?, evidence_source=?, "
                "source_agent=?, last_updated=? WHERE entity=? AND attribute=?",
                (new_value, new_conf, evidence, p["source_agent"], now, e, a),
            )
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                "old_confidence, new_confidence, reason, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (e, a, existing["value"], new_value, existing["confidence"], new_conf,
                 f"proposal #{p['id']} accepted by {by}: {reason}", now, by),
            )
            conn.commit()
            return FactWriteResult(
                written=True, was_contradiction=existing["value"] != new_value,
                previous_value=existing["value"], previous_confidence=existing["confidence"],
                reason=f"proposal #{p['id']} accepted", proposal_id=p["id"], authority=tier,
            )
        finally:
            conn.close()

    def _resolve_memory(self, conn: sqlite3.Connection, p: sqlite3.Row, action: str,
                        by: str, reason: str, now: float, apply_memory) -> FactWriteResult:
        """The memory-kind half of resolve_proposal; conn holds BEGIN IMMEDIATE."""
        kind, ref, attr = p["target_kind"], p["target_ref"], p["attribute"]
        if action == "accept" and kind != "memory_category":
            conn.rollback()
            return FactWriteResult(written=False, was_contradiction=False,
                                   reason="kind not promotable yet", proposal_id=p["id"])
        if action == "reject":
            old_value = p["current_value"]
            status, note = "rejected", "rejected"
        else:
            if apply_memory is None:
                conn.rollback()
                raise ValueError("a memory proposal needs apply_memory to be accepted")
            tenant, memory_id = parse_memory_target_ref(ref)
            # a failed apply rolls back and raises: the proposal stays pending
            try:
                old_value, undo = apply_memory(tenant, memory_id, p["proposed_value"], int(p["id"]))
            except Exception:
                conn.rollback()
                raise
            status, note = "accepted", "accepted"
        try:
            conn.execute(
                "UPDATE fact_proposals SET status=?, resolved_at=?, resolved_by=?, resolution_reason=? WHERE id=?",
                (status, now, by, reason, p["id"]),
            )
            conn.execute(
                "INSERT INTO fact_history (entity, attribute, old_value, new_value, "
                "old_confidence, new_confidence, reason, changed_at, changed_by) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                (ref, attr, old_value,
                 p["proposed_value"] if action == "accept" else old_value,
                 f"{kind} proposal #{p['id']} {note} by {by}: {reason}", now, by),
            )
            conn.commit()
        except Exception:
            # the memory moved but the proposal did not close: put it back,
            # so file and proposal never disagree
            conn.rollback()
            if action == "accept":
                try:
                    undo()
                except Exception as undo_exc:
                    log.error(f"Proposal #{p['id']}: close failed AND undo failed "
                              f"({type(undo_exc).__name__}) — {ref} holds the NEW "
                              f"category while the proposal stays pending")
            raise
        return FactWriteResult(
            written=action == "accept",
            was_contradiction=action == "accept" and old_value != p["proposed_value"],
            previous_value=old_value, reason=f"proposal #{p['id']} {note}",
            proposal_id=p["id"], authority="n/a",
        )

    # ── 4.26.0 Jev shadow counters ────────────────────────────────────────

    JEV_COUNTERS = ("eligible", "dropped", "attempted", "ok", "timeout",
                    "error", "agree", "disagree")

    def jev_bump(self, day: str, tenant: str, stored_category: str,
                 bucket: str = "-", **counts: int) -> None:
        """Add to one counter cell. Unknown counter names raise."""
        bad = set(counts) - set(self.JEV_COUNTERS)
        if bad:
            raise ValueError(f"unknown jev counter(s): {sorted(bad)}")
        if not counts:
            return
        cols = sorted(counts)
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO jev_shadow_stats (day, tenant, stored_category, bucket, "
                f"{', '.join(cols)}) VALUES (?, ?, ?, ?, {', '.join('?' * len(cols))}) "
                f"ON CONFLICT(day, tenant, stored_category, bucket) DO UPDATE SET "
                + ", ".join(f"{c}={c}+excluded.{c}" for c in cols),
                (day, tenant, stored_category, bucket, *(int(counts[c]) for c in cols)),
            )
            conn.commit()
        finally:
            conn.close()

    def jev_stats(self, since_day: str) -> list[dict]:
        """Every counter cell on or after `since_day` (YYYY-MM-DD)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jev_shadow_stats WHERE day >= ? "
                "ORDER BY day, tenant, stored_category, bucket", (since_day,)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def locked_for_prompt(self, prompt: str, limit: int = 3) -> list[Fact]:
        """Locked (probe/declared) facts whose entity the prompt names as a
        whole word, newest first. The read half of authority tiers: a hard
        fact about a named entity speaks before any soft memory."""
        # 4.24.0: the prompt is read the way the lexical lane reads it — a
        # media extension is a format, not a word of the entity's name, and a
        # CamelCase run is its words — so "where is RockLobster.mp3" names
        # the entity "rock lobster". Separators (-._/) become spaces on both
        # sides: "IGOR-2" and "igor 2" are one name.
        text = prompt_words(prompt)
        if not text.strip():
            return []
        limit = max(1, min(int(limit), 20))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM facts WHERE authority != 'open' AND confidence != 'false' "
                "ORDER BY last_updated DESC"
            ).fetchall()
        finally:
            conn.close()
        out: list[Fact] = []
        for row in rows:
            pattern = r"(?<![a-z0-9])" + re.escape(prompt_words(row["entity"])) + r"(?![a-z0-9])"
            if re.search(pattern, text):
                out.append(self._row_to_fact(row))
                if len(out) >= limit:
                    break
        return out
