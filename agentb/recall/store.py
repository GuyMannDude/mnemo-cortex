"""Mnemo Cortex Recall — SQLite FTS5 memory store.

Provides exact keyword search over structured memory records.
Complements Mnemo's vector/semantic search with precise text matching.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import MemoryRecord

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    date TEXT,
    confidence REAL,
    entities_json TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text,
    title,
    entities,
    content='memories',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text, title, entities)
    VALUES (new.id, new.text, coalesce(new.title, ''), coalesce(new.entities_json, '[]'));
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text, title, entities)
    VALUES ('delete', old.id, old.text, coalesce(old.title, ''), coalesce(old.entities_json, '[]'));
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text, title, entities)
    VALUES ('delete', old.id, old.text, coalesce(old.title, ''), coalesce(old.entities_json, '[]'));
    INSERT INTO memories_fts(rowid, text, title, entities)
    VALUES (new.id, new.text, coalesce(new.title, ''), coalesce(new.entities_json, '[]'));
END;
"""


def default_db_path(workspace: Path) -> Path:
    """Default location for the recall SQLite database."""
    return workspace / ".mnemo-recall" / "index.sqlite"


def connect(db_path: Path) -> sqlite3.Connection:
    """Connect to the recall database, creating schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def rebuild_index(conn: sqlite3.Connection, records: Iterable[MemoryRecord]) -> int:
    """Drop and rebuild the entire index from markdown source files."""
    conn.execute("DELETE FROM memories")
    count = 0
    for rec in records:
        conn.execute(
            """
            INSERT INTO memories(path, line_start, line_end, kind, title, text, date, confidence, entities_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.path.as_posix(),
                rec.line_start,
                rec.line_end,
                rec.kind,
                rec.title,
                rec.text,
                rec.date,
                rec.confidence,
                json.dumps(rec.entities),
            ),
        )
        count += 1
    conn.commit()
    return count


def append_record(conn: sqlite3.Connection, rec: MemoryRecord) -> int:
    """Append a single memory record to the index."""
    cur = conn.execute(
        """
        INSERT INTO memories(path, line_start, line_end, kind, title, text, date, confidence, entities_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec.path.as_posix(),
            rec.line_start,
            rec.line_end,
            rec.kind,
            rec.title,
            rec.text,
            rec.date,
            rec.confidence,
            json.dumps(rec.entities),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 8,
    since: str | None = None,
    entity: str | None = None,
) -> list[sqlite3.Row]:
    """Search memories using FTS5 full-text search with optional filters."""
    filters = []
    params: list[object] = []

    if since:
        filters.append("(memories.date IS NULL OR memories.date >= ?)")
        params.append(since)

    if entity:
        filters.append("EXISTS (SELECT 1 FROM json_each(memories.entities_json) WHERE value = ?)")
        params.append(entity)

    filters.append("memories_fts MATCH ?")
    params.append(query)

    where_sql = " AND ".join(filters)

    sql = f"""
    SELECT memories.*, bm25(memories_fts, 8.0, 2.0, 3.0) AS score
    FROM memories_fts
    JOIN memories ON memories.id = memories_fts.rowid
    WHERE {where_sql}
    ORDER BY score, coalesce(memories.date, '0000-00-00') DESC, memories.id DESC
    LIMIT ?
    """
    params.append(limit)
    return list(conn.execute(sql, params))


def recent_pack(conn: sqlite3.Connection, *, since: str | None, limit: int = 12) -> list[sqlite3.Row]:
    """Get recent memories for a /new recovery pack."""
    sql = """
    SELECT *
    FROM memories
    WHERE (? IS NULL OR date >= ?)
    ORDER BY coalesce(date, '0000-00-00') DESC, id DESC
    LIMIT ?
    """
    return list(conn.execute(sql, (since, since, limit)))
