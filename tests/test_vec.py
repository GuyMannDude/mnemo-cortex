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
