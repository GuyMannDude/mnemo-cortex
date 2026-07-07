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

import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

CONFIDENCE_LEVELS = ("false", "high_probability", "verified")
_CONFIDENCE_RANK = {c: i for i, c in enumerate(CONFIDENCE_LEVELS)}


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

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FactWriteResult:
    written: bool
    was_contradiction: bool
    previous_value: Optional[str] = None
    previous_confidence: Optional[str] = None
    reason: str = ""


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
    """

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
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(self.SCHEMA)
        return conn

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
                    reason="reasserted",
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
                    reason="overwritten by equal-or-higher confidence",
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
                reason="rejected — existing higher confidence takes precedence",
            )
        finally:
            conn.close()

    def demote(self, entity: str, attribute: str, reason: str, changed_by: Optional[str] = None) -> FactWriteResult:
        """Force a fact to confidence='false' without supplying a new value.

        Used when something is known wrong but the correct value isn't known yet.
        Required because the promotion ladder otherwise blocks verified→false
        transitions.
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
            if existing["confidence"] == "false":
                return FactWriteResult(
                    written=False, was_contradiction=False,
                    previous_confidence="false", reason="already false",
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
                reason="demoted",
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
                "(reason LIKE 'rejected%' OR reason LIKE 'contradicted%' OR reason LIKE 'demote%')"
                + since_clause +
                " ORDER BY changed_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
