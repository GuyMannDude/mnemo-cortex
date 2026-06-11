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

import httpx
import sqlite_vec

log = logging.getLogger("agentb.vec")

EMBED_DIM = 768  # nomic-embed-text
SCHEMA_VERSION = 2  # v2: vec_sources.category column (#468 category pushdown)

# nomic-embed-text accepts ~2048 tokens. For typical English prose that's
# ~6-8k chars, but path-heavy content (long file URIs, UUIDs, hash strings)
# tokenizes much denser — a 6000-char wiki FILE INDEX batch still 400'd
# on production data because the path tokens consumed more of the window
# than a chars-based estimate predicted. 4000 chars is conservative enough
# to survive the worst observed shapes while still retaining useful signal.
# Oversize entries get truncated with a warning; the truncated text is what
# lands in vec_sources so source and vector stay consistent.
MAX_EMBED_INPUT_CHARS = 4000


@dataclass
class VecHit:
    memory_id: str
    text: str
    distance: float
    source_file: Optional[str] = None
    created_at: Optional[float] = None
    category: Optional[str] = None


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
                created_at REAL NOT NULL,
                category TEXT
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                memory_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBED_DIM}]
            );

            -- v4.1: recall access tracking, feeds the composite ranking's
            -- access-frequency signal. Kept here (not in the memory JSONs)
            -- so serving a recall never rewrites a memory file.
            CREATE TABLE IF NOT EXISTS recall_stats (
                memory_id TEXT PRIMARY KEY,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed REAL
            );
        """)
        # v2 (#468): `category` column on an existing v1 table. Additive and
        # idempotent — old code ignores the column, search-without-category is
        # unchanged, so this is safe to run live. The column starts NULL on
        # existing rows; `backfill_categories` (migrate vec-backfill) populates
        # it from disk truth, and every upsert keeps it current thereafter.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(vec_sources)")}
        if "category" not in cols:
            self._conn.execute("ALTER TABLE vec_sources ADD COLUMN category TEXT")
        self._conn.execute(
            "INSERT OR REPLACE INTO vec_meta(key, value) VALUES (?, ?)",
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
        category: Optional[str] = None,
    ) -> None:
        """Insert or replace a memory's source text and embedding.

        `category` (#468) is the same value the memory JSON carries, promoted to
        a column so category-filtered search filters inside the kNN instead of
        reading every candidate's JSON. The handler's disk-truth filter stays the
        correctness authority — this column is a pre-filter for speed.
        """
        if len(embedding) != EMBED_DIM:
            raise VecDimMismatch(
                f"Expected embedding of dim {EMBED_DIM}, got {len(embedding)}. "
                f"memory_id={memory_id}. Refusing silent vector loss."
            )
        ts = created_at if created_at is not None else time.time()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO vec_sources(memory_id, text, source_file, created_at, category)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    text = excluded.text,
                    source_file = excluded.source_file,
                    created_at = excluded.created_at,
                    category = excluded.category
                """,
                (memory_id, text, source_file, ts, category),
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

    def update_category(self, memory_id: str, category: Optional[str]) -> None:
        """Refresh a memory's category column without re-embedding.

        Reclassification rewrites the JSON category but historically left
        vec_sources untouched (category was disk-only). Now that category is a
        search pre-filter column, a stale value would wrongly EXCLUDE a
        reclassified memory from category-filtered recall — a silent
        false-negative. Every reclassify path must call this so the column
        tracks disk truth. No-op if the memory isn't indexed.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE vec_sources SET category = ? WHERE memory_id = ?",
                (category, memory_id),
            )

    # ── Reads ──

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int = 8,
        include_category: Optional[str] = None,
        exclude_categories: Optional[Iterable[str]] = None,
        overfetch_multiplier: int = 5,
    ) -> list[VecHit]:
        """kNN search, optionally category-filtered inside the index (#468).

        Without a category filter this is the original top-k nearest search.

        With `include_category` and/or `exclude_categories`, the kNN fetches
        `top_k * overfetch_multiplier` candidates and filters them by the
        `category` column, returning the nearest `top_k` survivors. This keeps
        a session_log-dominated store from handing back an all-hidden top-k that
        forces the slow L3 disk-walk — the category-blindness this fixes.

        Filter semantics mirror the handler's metadata predicate exactly so the
        column stays a pure pre-filter (the disk-truth check remains authority):
          - include: keep only rows whose category == include_category
            (a NULL-category row can't satisfy a positive category filter)
          - exclude: drop rows whose category is in exclude_categories
            (a NULL-category row is NOT excluded — unknown ≠ hidden)
        If the filtered set is smaller than top_k, the partial set is returned —
        the caller must NOT fall through to L3 (partial beats a timeout).
        """
        if len(query_embedding) != EMBED_DIM:
            raise VecDimMismatch(
                f"Query embedding dim {len(query_embedding)} != index dim {EMBED_DIM}"
            )
        exclude = set(exclude_categories or ())
        filtering = bool(include_category) or bool(exclude)
        k = top_k * overfetch_multiplier if filtering else top_k
        rows = self._conn.execute(
            """
            SELECT s.memory_id, s.text, s.source_file, s.created_at, s.category, v.distance
            FROM vec_embeddings v
            JOIN vec_sources s ON s.memory_id = v.memory_id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (_serialize_vector(query_embedding), k),
        ).fetchall()
        hits: list[VecHit] = []
        for r in rows:
            cat = r["category"]
            if include_category is not None and cat != include_category:
                continue
            if cat is not None and cat in exclude:
                continue
            hits.append(
                VecHit(
                    memory_id=r["memory_id"],
                    text=r["text"],
                    distance=float(r["distance"]),
                    source_file=r["source_file"],
                    created_at=r["created_at"],
                    category=cat,
                )
            )
            if filtering and len(hits) >= top_k:
                break
        return hits

    # ── Recall access stats (v4.1, composite ranking signal) ──

    def bump_access(self, memory_ids: Iterable[str]) -> None:
        ids = [i for i in memory_ids if i]
        if not ids:
            return
        now = time.time()
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO recall_stats(memory_id, access_count, last_accessed)
                VALUES (?, 1, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    access_count = access_count + 1,
                    last_accessed = excluded.last_accessed
                """,
                [(i, now) for i in ids],
            )

    def access_counts(self, memory_ids: Iterable[str]) -> dict[str, int]:
        ids = [i for i in memory_ids if i]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT memory_id, access_count FROM recall_stats WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {r["memory_id"]: int(r["access_count"]) for r in rows}

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM vec_embeddings").fetchone()
        return int(row["n"])

    def get_embedding(self, memory_id: str) -> Optional[list[float]]:
        """Read back a stored vector (v4.1 — analyst dedup needs true cosine
        against existing memories, not a kNN distance heuristic)."""
        try:
            row = self._conn.execute(
                "SELECT embedding FROM vec_embeddings WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        except sqlite3.Error as e:
            log.warning(f"get_embedding({memory_id}) failed: {e}")
            return None
        if row is None:
            return None
        return _deserialize_vector(row["embedding"])

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


def _deserialize_vector(blob: bytes) -> list[float]:
    import struct
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


# ── Mode detection + backfill ──

def detect_mode(memory_dir: Path) -> str:
    """Return 'migration' if memory_dir has JSON entries, else 'clean'."""
    if not memory_dir.exists():
        return "clean"
    for _ in memory_dir.glob("*.json"):
        return "migration"
    return "clean"


def iter_memory_entries(
    memory_dir: Path,
) -> Iterable[tuple[str, str, Path, Optional[float], Optional[str]]]:
    """Yield (memory_id, canonical_text, source_path, created_at, category) per memory JSON.

    Canonical text matches what writeback embeds: summary + key_facts joined
    by newline. Texts longer than MAX_EMBED_INPUT_CHARS are truncated — the
    embedder's context window is finite and an oversize input would 400, trip
    the circuit breaker, and kill the rest of the run.
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
        if len(text) > MAX_EMBED_INPUT_CHARS:
            log.warning(
                f"Truncating oversize memory {memory_id} for embedding: "
                f"{len(text)} -> {MAX_EMBED_INPUT_CHARS} chars"
            )
            text = text[:MAX_EMBED_INPUT_CHARS]
        yield memory_id, text, path, entry.get("created_at"), entry.get("category")


async def embed_with_adaptive_truncation(
    embed: Callable[[str], Awaitable[list[float]]],
    text: str,
    *,
    min_chars: int = 500,
) -> tuple[list[float], str]:
    """Embed text. On a 400 (context-length) error, halve and retry.

    Returns (vector, text_actually_embedded). The returned text is what
    the caller should persist in vec_sources so the source row stays in
    sync with the vector that was actually computed.

    Why this exists: Ollama embedding endpoints reject inputs that exceed
    the model's context window with HTTP 400. The character-based cap in
    iter_memory_entries is a heuristic that breaks down on token-dense
    content (UUIDs, hash strings, file URIs). Adaptive halving handles
    the rest without tripping the embedder's circuit breaker.
    """
    current = text
    while True:
        try:
            return await embed(current), current
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and len(current) > min_chars:
                new_len = max(min_chars, len(current) // 2)
                log.warning(
                    f"Embed 400 at {len(current)} chars; retrying at {new_len}"
                )
                current = current[:new_len]
                continue
            raise


async def backfill(
    store: VecStore,
    memory_dir: Path,
    embed: Callable[[str], Awaitable[list[float]]],
    *,
    skip_existing: bool = True,
    progress_every: int = 50,
    adaptive: bool = True,
) -> dict:
    """Walk memory_dir, embed entries that aren't in the vec index, upsert.

    `adaptive=True` (default) retries on HTTP 400 with progressively shorter
    input — the safe path for production backfill. `adaptive=False` falls
    back to the raw embed call, used by tests with synthetic embedders.

    Returns a stats dict: {total, embedded, skipped, failed, elapsed_sec,
    truncated}.
    """
    start = time.time()
    total = 0
    embedded = 0
    skipped = 0
    failed = 0
    truncated = 0
    for memory_id, text, path, created_at, category in iter_memory_entries(memory_dir):
        total += 1
        if skip_existing and store.has(memory_id):
            skipped += 1
            continue
        try:
            if adaptive:
                vec, stored_text = await embed_with_adaptive_truncation(embed, text)
                if len(stored_text) < len(text):
                    truncated += 1
            else:
                vec = await embed(text)
                stored_text = text
            store.upsert(
                memory_id,
                stored_text,
                vec,
                source_file=path.as_posix(),
                created_at=created_at,
                category=category,
            )
            embedded += 1
        except Exception as e:
            failed += 1
            log.error(f"Backfill failed for {memory_id} ({path}): {e}")
        if total % progress_every == 0:
            log.info(
                f"Backfill progress: {total} seen, {embedded} embedded, "
                f"{skipped} skipped, {failed} failed, {truncated} adaptively truncated"
            )
    elapsed = time.time() - start
    log.info(
        f"Backfill done: {total} seen, {embedded} embedded, "
        f"{skipped} skipped, {failed} failed, {truncated} adaptively truncated, "
        f"{elapsed:.1f}s"
    )
    return {
        "total": total,
        "embedded": embedded,
        "skipped": skipped,
        "failed": failed,
        "truncated": truncated,
        "elapsed_sec": round(elapsed, 2),
    }


def backfill_categories(store: VecStore, memory_dir: Path) -> dict:
    """Populate vec_sources.category from disk truth for already-indexed rows.

    The #468 one-time deploy step: existing v1 stores have a NULL category
    column after the ALTER. This reads each indexed memory's category from its
    JSON and writes it to the column — NO embedding, just metadata, so it's fast
    and safe to run while the server is up (each UPDATE is a single row). Rows
    not on disk keep their existing value; the category-filtered search still
    disk-truths every survivor, so a missed row is at worst a slower fall-through,
    never a wrong result.

    Returns {indexed, updated, missing_json}.
    """
    start = time.time()
    updated = 0
    indexed = 0
    for path in sorted(memory_dir.glob("*.json")):
        try:
            entry = json.loads(path.read_text())
        except Exception as e:
            log.warning(f"backfill_categories: skipping unreadable {path}: {e}")
            continue
        memory_id = entry.get("id") or path.stem
        if not store.has(memory_id):
            continue
        indexed += 1
        store.update_category(memory_id, entry.get("category"))
        updated += 1
    # vec rows with no JSON on disk (orphans) keep whatever category they had.
    # store.has() gates `indexed` 1:1 with a vec row (memory_id is the PK), so
    # this difference is exact, not an estimate.
    missing = max(0, store.count() - indexed)
    elapsed = time.time() - start
    log.info(
        f"Category backfill done: {indexed} indexed rows updated from disk, "
        f"{missing} indexed rows without a JSON on disk, {elapsed:.1f}s"
    )
    return {"indexed": indexed, "updated": updated, "missing_json": missing}
