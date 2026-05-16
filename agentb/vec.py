"""Mnemo Cortex sqlite-vec backed vector index (v4 Phase 2).

Per-agent SQLite database with two tables:
  - vec_sources: memory_id, text, source_file, created_at (rebuild-from-text source)
  - vec_embeddings: vec0 virtual table, FLOAT[768] (nomic-embed-text)

Auto-detected operating modes (decided at first init for a tenant):
  - migration: tenant memory_dir already has JSON entries on disk
  - clean: tenant memory_dir is empty

Migration mode schedules a one-shot backfill that re-embeds existing memory
entries. Clean mode just initializes an empty index. New writes flow into
the same vec0 table either way.

Dimension is locked to 768 (nomic-embed-text). Mismatched-dim vectors are
rejected at insert time and surfaced to the caller — silent vector loss is
worse than a loud crash (Vapor Truth).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

import sqlite_vec

log = logging.getLogger("agentb.vec")

EMBED_DIM = 768  # nomic-embed-text
SCHEMA_VERSION = 1


@dataclass
class VecHit:
    memory_id: str
    text: str
    distance: float
    source_file: Optional[str] = None
    created_at: Optional[float] = None


class VecDimMismatch(ValueError):
    """Raised when a write attempts to insert a vector of the wrong dimension."""


class VecStore:
    """Per-tenant sqlite-vec index over memory entries."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _ensure_schema(self) -> None:
        self._conn.executescript(f"""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS vec_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vec_sources (
                memory_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                source_file TEXT,
                created_at REAL NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                memory_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBED_DIM}]
            );
        """)
        self._conn.execute(
            "INSERT OR IGNORE INTO vec_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO vec_meta(key, value) VALUES (?, ?)",
            ("embed_dim", str(EMBED_DIM)),
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Writes ──

    def upsert(
        self,
        memory_id: str,
        text: str,
        embedding: list[float],
        *,
        source_file: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> None:
        """Insert or replace a memory's source text and embedding."""
        if len(embedding) != EMBED_DIM:
            raise VecDimMismatch(
                f"Expected embedding of dim {EMBED_DIM}, got {len(embedding)}. "
                f"memory_id={memory_id}. Refusing silent vector loss."
            )
        ts = created_at if created_at is not None else time.time()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO vec_sources(memory_id, text, source_file, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    text = excluded.text,
                    source_file = excluded.source_file,
                    created_at = excluded.created_at
                """,
                (memory_id, text, source_file, ts),
            )
            self._conn.execute(
                "DELETE FROM vec_embeddings WHERE memory_id = ?",
                (memory_id,),
            )
            self._conn.execute(
                "INSERT INTO vec_embeddings(memory_id, embedding) VALUES (?, ?)",
                (memory_id, _serialize_vector(embedding)),
            )

    def delete(self, memory_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM vec_sources WHERE memory_id = ?", (memory_id,))
            self._conn.execute("DELETE FROM vec_embeddings WHERE memory_id = ?", (memory_id,))

    # ── Reads ──

    def search(self, query_embedding: list[float], *, top_k: int = 8) -> list[VecHit]:
        if len(query_embedding) != EMBED_DIM:
            raise VecDimMismatch(
                f"Query embedding dim {len(query_embedding)} != index dim {EMBED_DIM}"
            )
        rows = self._conn.execute(
            """
            SELECT s.memory_id, s.text, s.source_file, s.created_at, v.distance
            FROM vec_embeddings v
            JOIN vec_sources s ON s.memory_id = v.memory_id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (_serialize_vector(query_embedding), top_k),
        ).fetchall()
        return [
            VecHit(
                memory_id=r["memory_id"],
                text=r["text"],
                distance=float(r["distance"]),
                source_file=r["source_file"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM vec_embeddings").fetchone()
        return int(row["n"])

    def has(self, memory_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM vec_embeddings WHERE memory_id = ? LIMIT 1",
            (memory_id,),
        ).fetchone()
        return row is not None

    def missing_ids(self, candidate_ids: Iterable[str]) -> list[str]:
        ids = list(candidate_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT memory_id FROM vec_embeddings WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchall()
        present = {r["memory_id"] for r in rows}
        return [i for i in ids if i not in present]


def _serialize_vector(vec: list[float]) -> bytes:
    """sqlite-vec accepts vectors as little-endian float32 byte blobs."""
    import struct
    return struct.pack(f"<{len(vec)}f", *vec)


# ── Mode detection + backfill ──

def detect_mode(memory_dir: Path) -> str:
    """Return 'migration' if memory_dir has JSON entries, else 'clean'."""
    if not memory_dir.exists():
        return "clean"
    for _ in memory_dir.glob("*.json"):
        return "migration"
    return "clean"


def iter_memory_entries(memory_dir: Path) -> Iterable[tuple[str, str, Path, Optional[float]]]:
    """Yield (memory_id, canonical_text, source_path, created_at) for each memory JSON.

    Canonical text matches what writeback embeds: summary + key_facts joined by newline.
    """
    for path in sorted(memory_dir.glob("*.json")):
        try:
            entry = json.loads(path.read_text())
        except Exception as e:
            log.warning(f"Skipping malformed memory file {path}: {e}")
            continue
        memory_id = entry.get("id") or path.stem
        summary = entry.get("summary", "") or ""
        key_facts = entry.get("key_facts") or []
        text = summary + "\n" + "\n".join(key_facts) if key_facts else summary
        text = text.strip()
        if not text:
            continue
        yield memory_id, text, path, entry.get("created_at")


async def backfill(
    store: VecStore,
    memory_dir: Path,
    embed: Callable[[str], Awaitable[list[float]]],
    *,
    skip_existing: bool = True,
    progress_every: int = 50,
) -> dict:
    """Walk memory_dir, embed entries that aren't in the vec index, upsert.

    Returns a stats dict: {total, embedded, skipped, failed, elapsed_sec}.
    """
    start = time.time()
    total = 0
    embedded = 0
    skipped = 0
    failed = 0
    for memory_id, text, path, created_at in iter_memory_entries(memory_dir):
        total += 1
        if skip_existing and store.has(memory_id):
            skipped += 1
            continue
        try:
            vec = await embed(text)
            store.upsert(
                memory_id,
                text,
                vec,
                source_file=path.as_posix(),
                created_at=created_at,
            )
            embedded += 1
        except Exception as e:
            failed += 1
            log.error(f"Backfill failed for {memory_id} ({path}): {e}")
        if total % progress_every == 0:
            log.info(
                f"Backfill progress: {total} seen, {embedded} embedded, "
                f"{skipped} skipped, {failed} failed"
            )
    elapsed = time.time() - start
    log.info(
        f"Backfill done: {total} seen, {embedded} embedded, "
        f"{skipped} skipped, {failed} failed, {elapsed:.1f}s"
    )
    return {
        "total": total,
        "embedded": embedded,
        "skipped": skipped,
        "failed": failed,
        "elapsed_sec": round(elapsed, 2),
    }
