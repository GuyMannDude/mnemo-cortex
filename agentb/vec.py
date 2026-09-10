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
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

import httpx
import sqlite_vec

log = logging.getLogger("agentb.vec")

EMBED_DIM = 768  # nomic-embed-text
SCHEMA_VERSION = 3  # v3: bounded Analyst/Muse lens progress index

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


@dataclass
class LexHit:
    """One lexical-lane hit. `score` is positive, higher is better."""
    memory_id: str
    text: str
    score: float
    source_file: Optional[str] = None
    created_at: Optional[float] = None
    category: Optional[str] = None


# Words that carry no lexical evidence on their own. Deliberately short:
# BM25's idf already discounts common words, this list only keeps the
# MATCH expression from being ten OR-terms of filler. Anything a prompt
# would say about ITSELF ("what did we decide") rather than about the memory.
_LEX_STOPWORDS = frozenset("""
a an the and or but if then so of to in on at by for from with without into
onto over under about as is are was were be been being am do does did done
have has had having will would shall should can could may might must
what which who whom whose when where why how this that these those there
here it its it's we our us you your they them their he she his her i me my
not no yes any all some each every much many more most very just also
than too again ever never now still yet only even
""".split())
_LEX_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
LEX_MAX_TERMS = 12
LEX_SCHEMA = 1  # bump to force every index to rebuild its lexical table on next open
# Pruning (2026-09-10): a term present in most of the store is what makes
# the FTS5 MATCH slow (every matching row is BM25-scored, LIMIT does not
# bound the work — 87 ms for eight common words on a 17k-row tenant vs
# 0.2 ms for a rare identifier) and is exactly what BM25's idf values least.
# A term is dropped when its document count exceeds LEX_COMMON_FRACTION of
# the rows AND LEX_COMMON_MIN rows — the floor keeps a small store (the
# recall harnesses' 44 and 75 memories) unpruned, so every term still counts
# there. A prompt of only common words yields no lexical evidence at all,
# which is the honest answer: those words pick nothing out.
LEX_COMMON_FRACTION = 0.10
LEX_COMMON_MIN = 50
# Exact identifiers (4.21.1): an identifier-shaped term that appears in at
# most LEX_EXACT_MAX_DF memories is a PIN, not a topic — a commit hash, an
# advisory id, a CVE. The memory that holds it is served first (server.py),
# because on a real tenant a long, diffuse memory that carries the hash
# sits far below the pool's top on cosine and no honest lexical weight
# lifts it (live smoke 2026-09-10: the cc tenant, 'what happened with
# commit 379f571', 20 vector chunks served, the one memory holding the
# hash absent — CC2's diagnostic put it at lexical rank 19, cut by the
# lane's own top_k before the ranker saw it).
# Shape: letters AND digits at 4+ chars ('379f571', '7q2x', 'c6xx'), or
# all digits at 5+ ('50001', '40412') — a bare 4-digit number ('2000
# words', a year) pinned an unrelated memory in review, so it is out; the
# 4-digit ports ('9137') go with it, the honest trade. The df cap is 25,
# not 5: auto-capture echoes every query and bus receipt that mentions a
# hash, so a discussed identifier sits in a dozen memories and is still
# the thing the prompt named. A year or a port everyone mentions is in
# hundreds and stays a topic. The served window is protected separately
# (ranking.exact_first limit): pins re-order it, never own it.
LEX_EXACT_MAX_DF = 25
LEX_EXACT_MIN_LEN = 4        # letters + digits
LEX_EXACT_MIN_LEN_DIGITS = 5  # digits only


def is_identifier_shaped(term: str) -> bool:
    """Letters and digits mixed at LEX_EXACT_MIN_LEN+, or digits only at
    LEX_EXACT_MIN_LEN_DIGITS+. A version string never qualifies: '4.20.2'
    tokenises to '4' '20' '2' (stated limit, 4.21.1)."""
    if not any(ch.isdigit() for ch in term):
        return False
    if term.isdigit():
        return len(term) >= LEX_EXACT_MIN_LEN_DIGITS
    return len(term) >= LEX_EXACT_MIN_LEN


def lexical_terms(prompt: str) -> list[str]:
    """Turn a prompt into the lexical lane's search terms, [] when nothing
    in it is worth matching.

    Tokenises the way FTS5's default unicode61 tokenizer does (runs of
    letters/digits; `-`, `.`, `_`, `/` all split) so "GHSA-8cw4-87c7-c6xx"
    becomes the same four tokens on both sides of the match. Drops
    stopwords and short filler (alpha < 3 chars; digit-bearing tokens keep
    from 2 chars so the "2" of "4.20.2" is dropped but "c6xx" stays). Terms are
    Digit-bearing tokens (ids, hashes, ports, versions) go first: they are
    the identifiers the lane exists for, and the term cap trims from the
    tail. VecStore.lexical_search turns the list into a MATCH expression —
    every term quoted, so no prompt character reaches the parser, and
    OR-ed: a prompt is a question, not a phrase, and the AND FTS5 applies
    by default would match nothing."""
    seen: set[str] = set()
    ids: list[str] = []
    words: list[str] = []
    for tok in _LEX_TOKEN.findall(prompt.lower()):
        if tok in seen or tok in _LEX_STOPWORDS:
            continue
        has_digit = any(ch.isdigit() for ch in tok)
        if len(tok) < (2 if has_digit else 3):
            continue
        seen.add(tok)
        (ids if has_digit else words).append(tok)
    return (ids + words)[:LEX_MAX_TERMS]


class VecDimMismatch(ValueError):
    """Raised when a write attempts to insert a vector of the wrong dimension."""


class VecStore:
    """Per-tenant sqlite-vec index over memory entries."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._ensure_schema()
        self._ensure_unit_norm()

    def _connect(self) -> sqlite3.Connection:
        # FastAPI lifespan setup and request handling may run on different
        # worker threads (notably TestClient, and some ASGI deployments).
        # sqlite is built in serialized mode; permit the connection to follow
        # the app while bounded dedup queries use their own worker connection.
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _ensure_unit_norm(self) -> int:
        """v4.18.4 one-time migration: rows written before vectors were
        normalised at the boundary keep whatever length the provider gave
        them. Against a unit query such a row sits at L2 ~|v| — behind every
        row written since, for ever. Scale every non-unit row in place (a
        pure vector op, no re-embedding) and stamp vec_meta so this scan
        runs once per index. Batched: each batch is its own transaction so
        the write lock is held for milliseconds, and the work is idempotent
        (a unit row is skipped), so a crash or a concurrent opener mid-way
        simply resumes on the next open. Cost is one read of every stored
        vector, once (measured ~3 s / 20k rows on the all-unit live case).
        Returns the number of rows fixed."""
        row = self._conn.execute("SELECT value FROM vec_meta WHERE key = 'unit_norm'").fetchone()
        if row is not None:
            return 0
        fixed = 0
        batch = 500
        ids = [r[0] for r in self._conn.execute("SELECT memory_id FROM vec_sources ORDER BY memory_id").fetchall()]
        for start in range(0, len(ids), batch):
            chunk = ids[start:start + batch]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT memory_id, embedding FROM vec_embeddings WHERE memory_id IN ({marks})", chunk).fetchall()
            with self._conn:
                for r in rows:
                    vec = _deserialize_vector(r["embedding"])
                    n = math.sqrt(sum(x * x for x in vec))
                    if n > 0.0 and abs(n - 1.0) > 1e-3:
                        self._conn.execute("DELETE FROM vec_embeddings WHERE memory_id = ?", (r["memory_id"],))
                        self._conn.execute("INSERT INTO vec_embeddings(memory_id, embedding) VALUES (?, ?)",
                                           (r["memory_id"], _serialize_vector(unit_vector(vec))))
                        fixed += 1
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO vec_meta(key, value) VALUES ('unit_norm', '1')")
        if fixed:
            log.warning(f"vec index {self.db_path}: normalised {fixed} pre-4.18.4 non-unit row(s) in place")
        return fixed

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

            CREATE INDEX IF NOT EXISTS idx_vec_sources_created ON vec_sources(created_at DESC);

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

            CREATE TABLE IF NOT EXISTS lens_processed (
                memory_id TEXT NOT NULL,
                lens TEXT NOT NULL,
                processed_at REAL NOT NULL,
                PRIMARY KEY(memory_id, lens)
            );

            -- v4.21: the lexical lane. FTS5 over the same text the vectors
            -- were built from, so an exact identifier (an advisory id, an
            -- error string, a file name, a commit hash) is found by the
            -- words it contains when the embedding geometry misses it. A
            -- standalone table (not external-content over vec_sources: that
            -- table's rowid is implicit and VACUUM may renumber it); the
            -- text is short and stored twice on purpose.
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_lex USING fts5(
                memory_id UNINDEXED,
                text
            );
            -- per-term document counts over vec_lex (an FTS5 index seek per
            -- lookup): lexical_search prunes the terms that match most of
            -- the store before running the MATCH — see LEX_COMMON_FRACTION.
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_lex_vocab USING fts5vocab('vec_lex', 'row');
        """)
        # v4.21: an index created before the lexical table has rows in
        # vec_sources and none in vec_lex; rebuild the lexical side from the
        # source text ONCE and stamp vec_meta, the way unit_norm is stamped.
        # The stamp is the guard: dedup opens a fresh VecStore on every
        # /writeback, and probing the FTS5 table's row count on each open
        # was measured at ~28 ms on a 17k-row tenant (review, 2026-09-10).
        # Bumping LEX_SCHEMA (a tokenizer change, say) forces one rebuild
        # everywhere; so does deleting the key by hand. A downgrade to 4.20
        # writes vec_sources without vec_lex — memories written under the
        # old binary lack lexical evidence until the key is cleared; they
        # are still found by the vectors (disclosed in the 4.21.0 notes).
        stamp = self._conn.execute(
            "SELECT value FROM vec_meta WHERE key = 'lex_schema'").fetchone()
        if stamp is None or stamp["value"] != str(LEX_SCHEMA):
            n_src = self._conn.execute("SELECT COUNT(*) AS n FROM vec_sources").fetchone()["n"]
            self._conn.execute("DELETE FROM vec_lex")
            self._conn.execute("INSERT INTO vec_lex(memory_id, text) SELECT memory_id, text FROM vec_sources")
            self._conn.execute(
                "INSERT OR REPLACE INTO vec_meta(key, value) VALUES ('lex_schema', ?)", (str(LEX_SCHEMA),))
            log.info(f"vec index {self.db_path}: lexical lane built over {n_src} row(s) (lex_schema {LEX_SCHEMA})")
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
            self._conn.execute("DELETE FROM vec_lex WHERE memory_id = ?", (memory_id,))
            self._conn.execute("INSERT INTO vec_lex(memory_id, text) VALUES (?, ?)", (memory_id, text))
            self._conn.execute(
                "DELETE FROM vec_embeddings WHERE memory_id = ?",
                (memory_id,),
            )
            self._conn.execute(
                "INSERT INTO vec_embeddings(memory_id, embedding) VALUES (?, ?)",
                (memory_id, _serialize_vector(unit_vector(embedding))),
            )

    def delete(self, memory_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM vec_sources WHERE memory_id = ?", (memory_id,))
            self._conn.execute("DELETE FROM vec_lex WHERE memory_id = ?", (memory_id,))
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

    def pending_lens_sources(self, lens: str, limit: int) -> list[tuple[str, str]]:
        """Return oldest indexed session logs not yet processed by a lens."""
        rows = self._conn.execute(
            """
            SELECT s.memory_id, s.source_file
            FROM vec_sources AS s
            LEFT JOIN lens_processed AS p
              ON p.memory_id = s.memory_id AND p.lens = ?
            WHERE s.category = 'session_log' AND p.memory_id IS NULL
            ORDER BY s.created_at ASC, s.memory_id ASC
            LIMIT ?
            """, (lens, max(0, int(limit))),
        ).fetchall()
        return [(r["memory_id"], r["source_file"] or "") for r in rows]

    def session_log_source_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM vec_sources WHERE category = 'session_log'"
        ).fetchone()
        return int(row["n"])

    def mark_lens_processed(self, memory_id: str, lens: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO lens_processed(memory_id, lens, processed_at) "
                "VALUES (?, ?, ?)", (memory_id, lens, time.time()),
            )

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
            (_serialize_vector(unit_vector(query_embedding)), k),
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

    def lexical_search(
        self,
        terms: list[str],
        *,
        top_k: int = 8,
        include_category: Optional[str] = None,
        exclude_categories: Optional[Iterable[str]] = None,
        overfetch_multiplier: int = 5,
        prune: bool = True,
    ) -> list[LexHit]:
        """The lexical lane (v4.21): BM25 over the stored text.

        `terms` come from lexical_terms(); terms that match most of the
        store are pruned first (LEX_COMMON_FRACTION), the rest are quoted
        and OR-ed into the MATCH expression, so prompt punctuation can never
        reach the parser. Hits come back best first with a POSITIVE score
        (FTS5's bm25() is negative-is-better; negated here so callers reason
        in one direction). Category filtering mirrors search() exactly —
        include must equal, exclude drops, NULL is never excluded — and
        over-fetches the same way so a filtered lane still fills.
        """
        # prune=False for terms already known rare (the exact pass): the
        # prune's COUNT(*) over the vec0 table was 97% of a single-term
        # seek's cost in review (2.9 ms of 3.0 on 17k rows).
        if prune:
            terms = self.prune_common_terms(terms)
        if not terms:
            return []
        # FTS5 escapes a quote inside a phrase by doubling it; the tokenizer
        # never emits one, but a caller handing terms in raw still gets a
        # string the parser cannot read as syntax.
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        exclude = set(exclude_categories or ())
        filtering = bool(include_category) or bool(exclude)
        k = top_k * overfetch_multiplier if filtering else top_k
        rows = self._conn.execute(
            """
            SELECT s.memory_id, s.text, s.source_file, s.created_at, s.category,
                   bm25(vec_lex) AS score
            FROM vec_lex
            JOIN vec_sources s ON s.memory_id = vec_lex.memory_id
            WHERE vec_lex MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (match, k),
        ).fetchall()
        hits: list[LexHit] = []
        for r in rows:
            cat = r["category"]
            if include_category is not None and cat != include_category:
                continue
            if cat is not None and cat in exclude:
                continue
            hits.append(
                LexHit(
                    memory_id=r["memory_id"],
                    text=r["text"],
                    score=-float(r["score"]),
                    source_file=r["source_file"],
                    created_at=r["created_at"],
                    category=cat,
                )
            )
            if filtering and len(hits) >= top_k:
                break
        return hits

    def term_doc_count(self, term: str) -> int:
        """How many memories contain `term` (one FTS5 vocab seek; 0 if none)."""
        row = self._conn.execute(
            "SELECT doc FROM vec_lex_vocab WHERE term = ?", (term,)).fetchone()
        return int(row["doc"]) if row else 0

    def exact_terms(self, terms: list[str]) -> list[str]:
        """The identifier-shaped terms among `terms` (see is_identifier_shaped)
        present in 1..LEX_EXACT_MAX_DF memories."""
        return [t for t in terms
                if is_identifier_shaped(t) and 1 <= self.term_doc_count(t) <= LEX_EXACT_MAX_DF]

    def prune_common_terms(self, terms: list[str]) -> list[str]:
        """Drop the terms whose document count exceeds LEX_COMMON_FRACTION of
        the store (past the LEX_COMMON_MIN floor). One index seek per term."""
        if not terms:
            return []
        ceiling = max(LEX_COMMON_MIN, LEX_COMMON_FRACTION * self.count())
        return [t for t in terms if self.term_doc_count(t) <= ceiling]

    def newest(
        self,
        query_embedding: list[float],
        *,
        n: int,
        include_category: Optional[str] = None,
        exclude_categories: Optional[Iterable[str]] = None,
    ) -> list[VecHit]:
        """The `n` most recently created rows, with each row's L2 distance
        to the (unit-normalised) query computed here — the `recent` lens's
        retrieval leg (v4.18.4). A kNN pool holds the nearest neighbours;
        on a store of thousands of near-identical session logs the newest
        one is routinely outside it. Category filters mirror search() and
        are applied IN SQL, before the LIMIT. Two statements on purpose: a
        join against vec0 made SQLite scan every embedding blob (review
        2026-09-05: 2.7 s on 20k rows vs 4 ms for the metadata alone)."""
        if len(query_embedding) != EMBED_DIM:
            raise VecDimMismatch(
                f"Query embedding dim {len(query_embedding)} != index dim {EMBED_DIM}"
            )
        q = unit_vector(query_embedding)
        where, params = [], []
        if include_category is not None:
            where.append("s.category = ?"); params.append(include_category)
        exclude = list(exclude_categories or ())
        if exclude:
            where.append(f"(s.category IS NULL OR s.category NOT IN ({','.join('?' * len(exclude))}))")
            params.extend(exclude)
        sql = "SELECT s.memory_id, s.text, s.source_file, s.created_at, s.category FROM vec_sources s"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.created_at DESC, s.memory_id DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, n)).fetchall()
        if not rows:
            return []
        ids = [r["memory_id"] for r in rows]
        blobs = {r["memory_id"]: r["embedding"] for r in self._conn.execute(
            f"SELECT memory_id, embedding FROM vec_embeddings WHERE memory_id IN ({','.join('?' * len(ids))})",
            ids).fetchall()}
        hits: list[VecHit] = []
        for r in rows:
            blob = blobs.get(r["memory_id"])
            if blob is None:
                continue
            vec = _deserialize_vector(blob)
            d = math.sqrt(sum((a - b) * (a - b) for a, b in zip(q, vec)))
            hits.append(VecHit(memory_id=r["memory_id"], text=r["text"], distance=d,
                               source_file=r["source_file"], created_at=r["created_at"], category=r["category"]))
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


def unit_vector(vec: list[float]) -> list[float]:
    """Scale a vector to unit length (a zero vector is returned unchanged).

    v4.18.4 (E1, proving ground): the index stores and queries vec0 with L2
    distance, and the ranker maps that distance back to cosine assuming BOTH
    sides are unit vectors (ranking._to_cosine: cos = 1 - d^2/2). Ollama's
    nomic-embed-text happens to return unit vectors, so this held in
    production by luck of the provider. fastembed's nomic-embed-text-v1.5
    returns norm ~20: every L2 distance came out 8-17, every cosine clamped
    to -1, the pool normaliser scored every hit 1.0 and similarity went
    inert — recency and access count alone decided the order (the S289
    finding, reproduced on a 4-memory store). Normalising at THIS boundary
    makes the cosine mapping exact for any provider; on already-unit
    vectors it is a no-op, so the live index is unchanged."""
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0.0:
        return vec
    return [x / n for x in vec]


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
            entry = json.loads(path.read_text(encoding="utf-8"))
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
            entry = json.loads(path.read_text(encoding="utf-8"))
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
