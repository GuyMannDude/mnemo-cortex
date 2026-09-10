"""Tests for the sqlite-vec backed vector index (Mnemo v4 Phase 2)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Awaitable, Callable

import pytest

import httpx

from agentb.vec import (
    EMBED_DIM,
    MAX_EMBED_INPUT_CHARS,
    VecDimMismatch,
    VecStore,
    backfill,
    detect_mode,
    embed_with_adaptive_truncation,
    iter_memory_entries,
)


def _vec_along(axis: int, magnitude: float = 1.0) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[axis] = magnitude
    return v


def test_store_init_and_count(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    assert store.count() == 0
    assert (tmp_path / "vec.sqlite").exists()


def test_upsert_and_search_returns_nearest(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "hotdogs make me fart", _vec_along(0))
    store.upsert("m2", "completely different topic", _vec_along(1))
    store.upsert("m3", "another unrelated thing", _vec_along(2))

    hits = store.search(_vec_along(0), top_k=2)
    assert hits[0].memory_id == "m1"
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert len(hits) == 2


def test_vectors_are_normalised_at_the_boundary(tmp_path: Path):
    # E1 (proving ground, 2026-09-05): the ranker maps vec0's L2 distance back
    # to cosine assuming unit vectors on both sides. fastembed's nomic returns
    # norm ~20, so every distance was 8-17, every cosine clamped to -1 and the
    # whole pool collapsed to a similarity tie. A scaled vector must report
    # the SAME distance as its unit twin, on insert and on query.
    import math
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("unit", "unit", _vec_along(0))
    store.upsert("big", "big", _vec_along(1, magnitude=20.0))
    # 45 degrees between axis 0 and 1, scaled: cos = 0.7071 either way
    q = [0.0] * EMBED_DIM
    q[0] = q[1] = 15.0
    hits = {h.memory_id: h.distance for h in store.search(q, top_k=2)}
    expected = math.sqrt(2.0 - 2.0 * math.cos(math.pi / 4))
    assert hits["unit"] == pytest.approx(expected, abs=1e-5)
    assert hits["big"] == pytest.approx(expected, abs=1e-5)
    # and a far hit is FAR, not a tie at the clamp
    store.upsert("far", "far", _vec_along(2, magnitude=20.0))
    far = {h.memory_id: h.distance for h in store.search(q, top_k=3)}["far"]
    assert far == pytest.approx(math.sqrt(2.0), abs=1e-5)
    # the stored vector itself is unit length
    assert math.sqrt(sum(x * x for x in store.get_embedding("big"))) == pytest.approx(1.0, abs=1e-5)


def test_pre_existing_non_unit_rows_are_normalised_once_on_open(tmp_path: Path):
    # review 2026-09-05: an index written before 4.18.4 by a non-normalising
    # provider keeps norm-20 rows; against unit queries they sit at L2 ~20,
    # behind every row written since, for ever. Opening the store fixes them
    # in place (no re-embed) and stamps vec_meta so the scan runs once.
    import math, sqlite3
    from agentb.vec import _serialize_vector
    db = tmp_path / "vec.sqlite"
    store = VecStore(db)
    store.upsert("unit", "unit", _vec_along(0))
    # sneak a raw, un-normalised row in underneath the boundary
    with store._conn:
        store._conn.execute("INSERT INTO vec_sources(memory_id, text, created_at) VALUES ('raw', 'raw', 1.0)")
        store._conn.execute("INSERT INTO vec_embeddings(memory_id, embedding) VALUES (?, ?)",
                            ("raw", _serialize_vector(_vec_along(1, magnitude=20.0))))
        store._conn.execute("DELETE FROM vec_meta WHERE key = 'unit_norm'")
    store.close()
    store = VecStore(db)     # migration runs here
    q = [0.0] * EMBED_DIM
    q[0] = q[1] = 1.0
    d = {h.memory_id: h.distance for h in store.search(q, top_k=2)}
    expected = math.sqrt(2.0 - 2.0 * math.cos(math.pi / 4))
    assert d["raw"] == pytest.approx(expected, abs=1e-5)
    assert d["unit"] == pytest.approx(expected, abs=1e-5)
    assert store._conn.execute("SELECT value FROM vec_meta WHERE key='unit_norm'").fetchone()[0] == "1"
    assert store._ensure_unit_norm() == 0      # stamped: a second open scans nothing


def test_upsert_replaces_existing(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "old text", _vec_along(0))
    store.upsert("m1", "new text", _vec_along(1))
    assert store.count() == 1
    hits = store.search(_vec_along(1), top_k=1)
    assert hits[0].text == "new text"


def test_dim_mismatch_rejected_loudly(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    with pytest.raises(VecDimMismatch):
        store.upsert("bad", "x", [0.1, 0.2, 0.3])
    with pytest.raises(VecDimMismatch):
        store.search([0.1, 0.2, 0.3])
    # Failed write must not leave a partial source row behind.
    assert store.count() == 0
    assert not store.has("bad")


def test_delete_removes_both_tables(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "text", _vec_along(0))
    assert store.has("m1")
    store.delete("m1")
    assert not store.has("m1")
    assert store.count() == 0


def test_missing_ids_returns_unindexed(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "t", _vec_along(0))
    missing = store.missing_ids(["m1", "m2", "m3"])
    assert set(missing) == {"m2", "m3"}


def test_detect_mode_clean_when_no_json(tmp_path: Path):
    (tmp_path / "memory").mkdir()
    assert detect_mode(tmp_path / "memory") == "clean"


def test_detect_mode_migration_when_json_present(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "abc.json").write_text("{}")
    assert detect_mode(mem) == "migration"


def test_detect_mode_clean_when_dir_missing(tmp_path: Path):
    assert detect_mode(tmp_path / "no-such-dir") == "clean"


def test_iter_memory_entries_uses_summary_plus_key_facts(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "a.json").write_text(json.dumps({
        "id": "a",
        "summary": "core summary",
        "key_facts": ["fact one", "fact two"],
        "created_at": 123.0,
        "category": "topology",
    }))
    entries = list(iter_memory_entries(mem))
    assert len(entries) == 1
    mid, text, path, created_at, category = entries[0]
    assert mid == "a"
    assert text == "core summary\nfact one\nfact two"
    assert created_at == 123.0
    assert category == "topology"


def test_iter_memory_entries_skips_empty(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "empty.json").write_text(json.dumps({"id": "e", "summary": "", "key_facts": []}))
    (mem / "good.json").write_text(json.dumps({"id": "g", "summary": "real"}))
    entries = list(iter_memory_entries(mem))
    assert [e[0] for e in entries] == ["g"]


def test_iter_memory_entries_truncates_oversize(tmp_path: Path, caplog):
    """Oversize entries (e.g. wiki FILE INDEX batches) must NOT 400 the embedder
    and trip the circuit breaker. Truncation keeps the run alive."""
    mem = tmp_path / "memory"
    mem.mkdir()
    huge_summary = "x" * (MAX_EMBED_INPUT_CHARS + 5000)
    (mem / "huge.json").write_text(json.dumps({"id": "h", "summary": huge_summary}))
    with caplog.at_level("WARNING", logger="agentb.vec"):
        entries = list(iter_memory_entries(mem))
    assert len(entries) == 1
    _, text, _, _, _ = entries[0]
    assert len(text) == MAX_EMBED_INPUT_CHARS
    assert any("Truncating oversize memory h" in r.message for r in caplog.records)


def test_iter_memory_entries_tolerates_corrupt(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "broken.json").write_text("not json {")
    (mem / "good.json").write_text(json.dumps({"id": "g", "summary": "real"}))
    entries = list(iter_memory_entries(mem))
    assert [e[0] for e in entries] == ["g"]


def _make_embedder(axis_for: Callable[[str], int]) -> Callable[[str], Awaitable[list[float]]]:
    async def _embed(text: str) -> list[float]:
        return _vec_along(axis_for(text))
    return _embed


@pytest.mark.asyncio
async def test_backfill_embeds_each_entry_once(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    mem.mkdir()
    for i in range(3):
        (mem / f"m{i}.json").write_text(json.dumps({
            "id": f"m{i}",
            "summary": f"entry {i}",
            "created_at": time.time(),
        }))

    embed = _make_embedder(lambda t: int(t.split()[-1]))
    stats = await backfill(store, mem, embed)
    assert stats["total"] == 3
    assert stats["embedded"] == 3
    assert stats["skipped"] == 0
    assert stats["failed"] == 0
    assert stats["truncated"] == 0
    assert store.count() == 3


@pytest.mark.asyncio
async def test_backfill_is_idempotent(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "a.json").write_text(json.dumps({"id": "a", "summary": "one"}))

    embed = _make_embedder(lambda t: 0)
    first = await backfill(store, mem, embed)
    second = await backfill(store, mem, embed)
    assert first["embedded"] == 1
    assert second["embedded"] == 0
    assert second["skipped"] == 1


@pytest.mark.asyncio
async def test_backfill_continues_past_failures(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "a.json").write_text(json.dumps({"id": "a", "summary": "one"}))
    (mem / "b.json").write_text(json.dumps({"id": "b", "summary": "two"}))

    async def flaky(text: str) -> list[float]:
        if "one" in text:
            raise RuntimeError("simulated embed failure")
        return _vec_along(0)

    stats = await backfill(store, mem, flaky)
    assert stats["embedded"] == 1
    assert stats["failed"] == 1
    assert store.count() == 1


def _http_400() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://localhost/api/embed")
    resp = httpx.Response(400, request=req, text='{"error":"context length"}')
    return httpx.HTTPStatusError("400", request=req, response=resp)


@pytest.mark.asyncio
async def test_adaptive_truncation_halves_on_400():
    calls: list[int] = []

    async def embed(text: str) -> list[float]:
        calls.append(len(text))
        if len(text) > 1000:
            raise _http_400()
        return _vec_along(0)

    vec, used = await embed_with_adaptive_truncation(embed, "x" * 8000, min_chars=200)
    assert vec == _vec_along(0)
    assert len(used) <= 1000
    # 8000 -> 4000 -> 2000 -> 1000 -> succeeds
    assert calls == [8000, 4000, 2000, 1000]


@pytest.mark.asyncio
async def test_adaptive_truncation_gives_up_at_min_chars():
    async def embed(text: str) -> list[float]:
        raise _http_400()

    with pytest.raises(httpx.HTTPStatusError):
        await embed_with_adaptive_truncation(embed, "x" * 8000, min_chars=500)


@pytest.mark.asyncio
async def test_adaptive_truncation_propagates_non_400(tmp_path: Path):
    async def embed(text: str) -> list[float]:
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        await embed_with_adaptive_truncation(embed, "hello world")


@pytest.mark.asyncio
async def test_backfill_counts_adaptive_truncations(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "huge.json").write_text(json.dumps({"id": "h", "summary": "x" * 2000}))
    (mem / "fine.json").write_text(json.dumps({"id": "f", "summary": "y" * 100}))

    async def embed(text: str) -> list[float]:
        # 400 on long "x" content; succeeds on short content or "y" content.
        if "x" in text and len(text) > 500:
            raise _http_400()
        return _vec_along(0)

    stats = await backfill(store, mem, embed)
    assert stats["embedded"] == 2
    assert stats["truncated"] == 1
    assert stats["failed"] == 0


def test_semantic_hit_where_keywords_miss(tmp_path: Path):
    """Canonical scenario from mnemo-v4-research.md Addition 1.

    With FTS5, the search 'hotdogs art' does NOT find 'hotdogs make me fart'.
    Vector similarity (over real embeddings) should — but in this test we
    simulate that by giving the related sentences close vectors and the
    unrelated sentences far ones. The store proves it returns the related
    memory by vector proximity even though keywords don't overlap.
    """
    store = VecStore(tmp_path / "vec.sqlite")

    near = [0.0] * EMBED_DIM
    near[:3] = [0.9, 0.1, 0.05]
    far1 = [0.0] * EMBED_DIM
    far1[:3] = [-0.9, 0.1, 0.05]
    far2 = [0.0] * EMBED_DIM
    far2[:3] = [0.05, -0.9, 0.1]

    store.upsert("m_related", "hotdogs make me fart", near)
    store.upsert("m_other_1", "completely unrelated phrase", far1)
    store.upsert("m_other_2", "another unrelated phrase", far2)

    query = [0.0] * EMBED_DIM
    query[:3] = [0.88, 0.12, 0.06]  # close to 'near', not overlapping any keywords
    hits = store.search(query, top_k=3)
    assert hits[0].memory_id == "m_related"
    assert hits[0].distance < hits[1].distance


# ── #468: category column + category-filtered search ──

def test_upsert_stores_category_and_search_returns_it(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "topology note", _vec_along(0), category="topology")
    hit = store.search(_vec_along(0), top_k=1)[0]
    assert hit.category == "topology"


def test_search_without_filter_is_unchanged(tmp_path: Path):
    """No category filter → original behaviour: plain top-k, no over-fetch."""
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("a", "x", _vec_along(0), category="session_log")
    store.upsert("b", "y", _vec_along(1), category="topology")
    hits = store.search(_vec_along(0), top_k=2)
    assert {h.memory_id for h in hits} == {"a", "b"}


def test_search_include_category_filters_in_index(tmp_path: Path):
    """The session_log-dominated store: a tiny top-k would be all session_log,
    but the include filter over-fetches and returns the on-category hits."""
    store = VecStore(tmp_path / "vec.sqlite")
    # 9 session_log near axis 0; one topology sitting mid-pack (rank ~4) so it's
    # inside the top_k*multiplier over-fetch window but NOT in a tiny top-k.
    for i in range(9):
        v = _vec_along(0, magnitude=1.0)
        v[1] = 0.01 * (i + 1)  # distances 0.01..0.09, all near-but-distinct
        store.upsert(f"log{i}", "log", v, category="session_log")
    topo_v = _vec_along(0, magnitude=1.0)
    topo_v[1] = 0.035  # between log2 (0.03) and log3 (0.04)
    store.upsert("topo", "the one topology memory", topo_v, category="topology")

    # top_k=1 with NO filter returns a session_log (nearest).
    assert store.search(_vec_along(0), top_k=1)[0].category == "session_log"
    # include_category=topology over-fetches (1*5=5 candidates) and finds it.
    hits = store.search(_vec_along(0), top_k=1, include_category="topology")
    assert len(hits) == 1
    assert hits[0].memory_id == "topo"


def test_search_exclude_categories_drops_hidden(tmp_path: Path):
    """Default recall (hide session_log): over-fetch + exclude yields the
    non-hidden hits instead of an all-session_log top-k that forces L3."""
    store = VecStore(tmp_path / "vec.sqlite")
    for i in range(9):
        v = _vec_along(0, magnitude=1.0)
        v[1] = 0.01 * i
        store.upsert(f"log{i}", "log", v, category="session_log")
    store.upsert("real", "a real memory", _vec_along(0, magnitude=0.9),
                 category="topology")

    hits = store.search(_vec_along(0), top_k=3, exclude_categories=["session_log"])
    assert [h.memory_id for h in hits] == ["real"]
    assert all(h.category != "session_log" for h in hits)


def test_search_exclude_keeps_null_category(tmp_path: Path):
    """A NULL-category row is unknown, not hidden — exclude must not drop it
    (mirrors the handler's `if category and category in exclude` semantics)."""
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("n", "no category", _vec_along(0))  # category defaults to None
    store.upsert("s", "session", _vec_along(1), category="session_log")
    hits = store.search(_vec_along(0), top_k=5, exclude_categories=["session_log"])
    ids = {h.memory_id for h in hits}
    assert "n" in ids and "s" not in ids


def test_search_include_returns_partial_when_thin(tmp_path: Path):
    """Thin category: fewer than top_k matches → return the partial set, never
    pad with off-category hits (the caller must not fall through to L3)."""
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("t", "only topology", _vec_along(0), category="topology")
    for i in range(5):
        store.upsert(f"log{i}", "log", _vec_along(i + 1), category="session_log")
    hits = store.search(_vec_along(0), top_k=8, include_category="topology")
    assert [h.memory_id for h in hits] == ["t"]


def test_update_category_refreshes_column(tmp_path: Path):
    """Reclassification path: category changes on disk → column must follow,
    or category-filtered search would wrongly exclude the memory."""
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m", "was unknown", _vec_along(0), category="unknown")
    assert store.search(_vec_along(0), top_k=1, include_category="topology") == []
    store.update_category("m", "topology")
    hits = store.search(_vec_along(0), top_k=1, include_category="topology")
    assert [h.memory_id for h in hits] == ["m"]


def test_update_category_noop_for_unindexed(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.update_category("ghost", "topology")  # must not raise
    assert store.count() == 0


def test_category_column_added_to_existing_v1_db(tmp_path: Path):
    """An existing v1 store (no category column) gets the column via ALTER on
    open — additive, non-destructive: existing rows survive with NULL category."""
    import sqlite3
    import sqlite_vec
    db = tmp_path / "vec.sqlite"
    conn = sqlite3.connect(str(db))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.executescript(
        """
        CREATE TABLE vec_sources (
            memory_id TEXT PRIMARY KEY, text TEXT NOT NULL,
            source_file TEXT, created_at REAL NOT NULL
        );
        CREATE VIRTUAL TABLE vec_embeddings USING vec0(
            memory_id TEXT PRIMARY KEY, embedding FLOAT[768]
        );
        """
    )
    conn.execute(
        "INSERT INTO vec_sources(memory_id, text, source_file, created_at) VALUES (?,?,?,?)",
        ("old", "legacy row", None, 1.0),
    )
    conn.commit()
    conn.close()

    store = VecStore(db)  # opening must ALTER in the category column, not crash
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(vec_sources)")}
    assert "category" in cols
    row = store._conn.execute(
        "SELECT category FROM vec_sources WHERE memory_id='old'"
    ).fetchone()
    assert row["category"] is None  # legacy row preserved, category NULL until backfill


@pytest.mark.asyncio
async def test_backfill_categories_populates_from_disk(tmp_path: Path):
    """The deploy step: a store indexed before the column existed gets its
    categories synced from the memory JSONs without re-embedding."""
    from agentb.vec import backfill_categories
    store = VecStore(tmp_path / "vec.sqlite")
    mem = tmp_path / "memory"
    mem.mkdir()
    # Index two memories with NULL category (simulating pre-#468 rows).
    store.upsert("a", "alpha", _vec_along(0))
    store.upsert("b", "beta", _vec_along(1))
    (mem / "a.json").write_text(json.dumps({"id": "a", "summary": "alpha", "category": "topology"}))
    (mem / "b.json").write_text(json.dumps({"id": "b", "summary": "beta", "category": "session_log"}))

    stats = backfill_categories(store, mem)
    assert stats["updated"] == 2
    assert store.search(_vec_along(0), top_k=1, include_category="topology")[0].memory_id == "a"
    excl = store.search(_vec_along(1), top_k=5, exclude_categories=["session_log"])
    assert "b" not in {h.memory_id for h in excl}


def test_newest_filters_in_sql_before_the_limit(tmp_path: Path):
    # review 2026-09-05: a Python-side filter after LIMIT n*5 returned zero
    # doctrines from a log-dominated store. The predicate is in the SQL now.
    store = VecStore(tmp_path / "vec.sqlite")
    for i in range(40):
        store.upsert(f"log{i}", "log", _vec_along(0), created_at=1000.0 + i, category="session_log")
    store.upsert("doc", "doctrine", _vec_along(1), created_at=1.0, category="doctrine")
    got = store.newest(_vec_along(0), n=3, include_category="doctrine")
    assert [h.memory_id for h in got] == ["doc"]
    got = store.newest(_vec_along(0), n=3, exclude_categories=["session_log"])
    assert [h.memory_id for h in got] == ["doc"]
    newest = store.newest(_vec_along(0), n=2)
    assert [h.memory_id for h in newest] == ["log39", "log38"]       # newest first, distance real
    assert newest[0].distance == pytest.approx(0.0, abs=1e-6)


# ── v4.21: the lexical lane ─────────────────────────────────────────────────

from agentb.vec import LexHit, LEX_COMMON_MIN, LEX_EXACT_MAX_DF, LEX_MAX_TERMS, lexical_terms  # noqa: E402


def _lex_rows(store: VecStore) -> dict[str, str]:
    return {r["memory_id"]: r["text"]
            for r in store._conn.execute("SELECT memory_id, text FROM vec_lex")}


def test_lexical_terms_tokenise_like_fts5_and_drop_filler():
    t = lexical_terms("what did we decide about GHSA-8cw4-87c7-c6xx in csv-parse?")
    # identifiers split on '-' the way unicode61 does, digit-bearing tokens first
    assert t[:3] == ["8cw4", "87c7", "c6xx"]
    assert {"decide", "ghsa", "csv", "parse"} <= set(t)
    assert not {"what", "did", "we", "about", "in"} & set(t)
    # only letter/digit runs survive: nothing a MATCH parser could read as syntax
    assert all(part.isalnum() for part in t)


def test_lexical_terms_empty_when_nothing_is_worth_matching():
    assert lexical_terms("what is it") == []
    assert lexical_terms("") == []
    assert lexical_terms("!!! --- ???") == []


def test_lexical_terms_cap_and_keep_identifiers_first():
    words = " ".join(f"alpha{chr(97 + i)}" for i in range(20))
    t = lexical_terms(f"{words} port 9137 hash 4b1d9e2")
    assert len(t) == LEX_MAX_TERMS
    assert t[:2] == ["9137", "4b1d9e2"]


def test_lexical_search_survives_fts5_syntax_in_the_prompt(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "the seal step failed with ECONNRESET on port 9137", _vec_along(0))
    # AND/OR/NOT, quotes, parens, colons, stars: all would be MATCH syntax raw
    hits = store.lexical_search(lexical_terms('port:9137 AND (NOT "seal") OR econnreset* -foo'), top_k=5)
    assert [h.memory_id for h in hits] == ["m1"]
    # and a term handed in raw (no tokenizer) is still quoted, never parsed
    assert store.lexical_search(['9137" OR "seal'], top_k=5) == []


def test_common_terms_are_pruned_past_the_floor(tmp_path: Path):
    """A term in most of the store is BM25's least valuable and the MATCH's
    most expensive; past the LEX_COMMON_MIN floor it is dropped before the
    query runs. Below the floor nothing is pruned (the harness worlds)."""
    store = VecStore(tmp_path / "vec.sqlite")
    n = LEX_COMMON_MIN * 2
    for i in range(n):
        store.upsert(f"m{i}", f"common restart note number {i}" + (" needle-7q2x" if i == 3 else ""),
                     _vec_along(i % EMBED_DIM))
    assert store.prune_common_terms(["restart", "7q2x", "unseen"]) == ["7q2x", "unseen"]
    assert [h.memory_id for h in store.lexical_search(["restart", "7q2x"], top_k=5)] == ["m3"]
    # prune=False skips the pruning (and its COUNT): the common term matches everything
    assert len(store.lexical_search(["restart"], top_k=5, prune=False)) == 5
    assert store.lexical_search(["restart", "common"], top_k=5) == []   # only common words: no evidence
    small = VecStore(tmp_path / "small.sqlite")
    for i in range(5):
        small.upsert(f"s{i}", "everyone says restart", _vec_along(i))
    assert small.prune_common_terms(["restart"]) == ["restart"]        # under the floor: kept


def test_upsert_and_delete_keep_the_lexical_row_in_step(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "advisory GHSA-8cw4 accepted on the csv reader", _vec_along(0))
    assert _lex_rows(store) == {"m1": "advisory GHSA-8cw4 accepted on the csv reader"}
    store.upsert("m1", "advisory GHSA-8cw4 RETRACTED", _vec_along(0))
    assert _lex_rows(store) == {"m1": "advisory GHSA-8cw4 RETRACTED"}  # replaced, not doubled
    store.delete("m1")
    assert _lex_rows(store) == {}


def test_pre_lexical_index_is_backfilled_once_on_open(tmp_path: Path):
    """An index built before v4.21 has vec_sources rows and no vec_lex table.
    Opening it must rebuild the lexical side from the stored text, once."""
    path = tmp_path / "vec.sqlite"
    store = VecStore(path)
    store.upsert("m1", "the memory server listens on port 50001", _vec_along(0))
    store.upsert("m2", "advisory GHSA-8cw4 accepted", _vec_along(1))
    store._conn.execute("DROP TABLE vec_lex")  # simulate the pre-4.21 schema:
    store._conn.execute("DELETE FROM vec_meta WHERE key = 'lex_schema'")  # no table, no stamp
    store._conn.commit()
    store.close()

    reopened = VecStore(path)
    assert _lex_rows(reopened) == {
        "m1": "the memory server listens on port 50001",
        "m2": "advisory GHSA-8cw4 accepted",
    }
    hits = reopened.lexical_search(lexical_terms("port 50001"), top_k=5)
    assert [h.memory_id for h in hits] == ["m1"]
    # a second open does NOT rebuild: a sentinel written into vec_lex survives
    # it (a rebuild would restore the source text). The stamp, not the row
    # count, is the guard — so this also pins that the probe is skipped.
    reopened._conn.execute("UPDATE vec_lex SET text = 'SENTINEL' WHERE memory_id = 'm1'")
    reopened._conn.commit()
    reopened.close()
    again = VecStore(path)
    assert _lex_rows(again)["m1"] == "SENTINEL"
    # clearing the stamp forces the rebuild (the lever for a tokenizer change)
    again._conn.execute("DELETE FROM vec_meta WHERE key = 'lex_schema'")
    again._conn.commit()
    again.close()
    rebuilt = VecStore(path)
    assert _lex_rows(rebuilt)["m1"] == "the memory server listens on port 50001"


def test_lexical_search_finds_the_identifier_the_vectors_cannot_see(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    # three memories on the same topic; only one carries the id in the prompt
    store.upsert("adv-a", "advisory GHSA-8cw4-87c7-c6xx in csv-parse accepted: branch unreachable",
                 _vec_along(0), category="decision")
    store.upsert("adv-b", "advisory GHSA-528h-pc64-c93x in stream-json accepted: local only",
                 _vec_along(1), category="decision")
    store.upsert("adv-c", "advisory GHSA-4w3w-2rp5-g8jm in xmldom accepted: dev-only chain",
                 _vec_along(2), category="decision")
    hits = store.lexical_search(lexical_terms("what did we decide on GHSA-8cw4-87c7-c6xx"), top_k=5)
    assert hits and hits[0].memory_id == "adv-a"
    assert isinstance(hits[0], LexHit) and hits[0].score > 0
    # the shared words ("advisory", "accepted") match the others too, but weaker
    assert all(h.score < hits[0].score for h in hits[1:])
    assert hits[0].category == "decision" and hits[0].created_at is not None


def test_lexical_search_category_filters_mirror_search(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("log", "port 9137 listener restarted", _vec_along(0), category="session_log")
    store.upsert("doc", "port 9137 is the bus listener", _vec_along(1), category="doctrine")
    store.upsert("nul", "port 9137 unknown provenance", _vec_along(2), category=None)
    q = lexical_terms("port 9137")
    assert {h.memory_id for h in store.lexical_search(q, top_k=5)} == {"log", "doc", "nul"}
    # exclude drops the hidden category and KEEPS the NULL row (unknown ≠ hidden)
    assert {h.memory_id for h in store.lexical_search(q, top_k=5, exclude_categories={"session_log"})} == {"doc", "nul"}
    # include: a NULL row cannot satisfy a positive filter
    assert [h.memory_id for h in store.lexical_search(q, top_k=5, include_category="doctrine")] == ["doc"]


def test_lexical_search_empty_match_is_a_no_op(tmp_path: Path):
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("m1", "anything", _vec_along(0))
    assert store.lexical_search([], top_k=5) == []


def test_exact_terms_are_rare_identifier_shaped_terms(tmp_path: Path):
    from agentb.vec import is_identifier_shaped
    store = VecStore(tmp_path / "vec.sqlite")
    # v4, 55 and 2000 are all PRESENT (df 1) so the shape rule, not the df rule, rejects them
    store.upsert("m1", "commit 379f571 fixed the seal; port 50001; v4 build 55 wrote 2000 rows in 2026",
                 _vec_along(0))
    for i in range(LEX_EXACT_MAX_DF + 1):
        store.upsert(f"y{i}", f"note {i} from 2026", _vec_along(i + 1))
    assert store.term_doc_count("379f571") == 1
    assert store.term_doc_count("2026") == LEX_EXACT_MAX_DF + 2
    assert store.term_doc_count("nowhere") == 0
    terms = lexical_terms("what happened with commit 379f571 on port 50001 in 2026, v4 build 55, 2000 rows")
    assert store.exact_terms(terms) == ["379f571", "50001"]  # 2026 too common; v4/55/2000 wrong shape; 'commit' no digit
    assert store.exact_terms(["c0ffee"]) == []   # shaped, but in NO memory: not a pin
    # the shape rule on its own
    assert is_identifier_shaped("7q2x") and is_identifier_shaped("379f571") and is_identifier_shaped("50001")
    assert not is_identifier_shaped("2000") and not is_identifier_shaped("9137")   # bare 4 digits
    assert not is_identifier_shaped("v4") and not is_identifier_shaped("55")
    assert not is_identifier_shaped("commit") and not is_identifier_shaped("20")
