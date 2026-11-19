"""POST /vec/backfill?prune_orphans=true removes the index rows whose record
is gone, archiving each first (item 14 C, CC rulings #6864 / #6878).

Item 14 A stopped recall serving such a row, 14 B counted them; nothing
removed them. The prune is off unless asked. Its row's text is the last copy
of that memory, so every row goes to <data_dir>/vec-orphans/<agent>-<utc>.jsonl
(fsynced) before any delete. Guards: a missing or empty memory folder refuses
always (a reindex question, never a prune); more than 25 % of the index
refuses unless force=true, and force overrides only that one. A data dir that
moved is not orphaned (recall's own record_file rule). Deletes go through
VecStore.delete_many, the one delete path (delete calls it): all three
tables, one transaction.

Also here (CC note_2 on 14 A): the analyst's dedup gate and the rulekeeper's
scan use the same rule as the writeback dedup gate: a row whose record is
gone is not a live twin, and a moved data dir still finds its records.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

import pytest

from tests.test_vec_orphan_count import VEC, _client, _save, _status

from agentb.analyst import analyze_tenant
from agentb.config import AnalysisConfig
from agentb.rulekeeper import scan_tenant
from agentb.vec import EMBED_DIM, OrphanPruneRefused, VecStore, prune_orphans


def _backfill(client, **params):
    return client.post("/vec/backfill", params={"agent_id": "cody", **params})


def _world(tmp_path, client, n: int, gone: int) -> list[str]:
    """n saved memories, the records of the first `gone` deleted by hand."""
    ids = [_save(client, f"tool {i} is on shelf {i}") for i in range(n)]
    for memory_id in ids[:gone]:
        (tmp_path / "cody" / "memory" / f"{memory_id}.json").unlink()
    return ids


def _vec(client) -> VecStore:
    return client.app.state.tenants.get("cody")["vec"]


def test_off_unless_asked(tmp_path):
    with _client(tmp_path / "cody") as client:
        _world(tmp_path, client, 4, 1)
        r = _backfill(client)
        assert r.status_code == 200 and "orphans_found" not in r.json()
        assert _status(client)["orphans"] == 1
        assert not (tmp_path / "cody" / "vec-orphans").exists()


def test_prune_archives_then_removes_every_table(tmp_path):
    with _client(tmp_path / "cody") as client:
        ids = _world(tmp_path, client, 4, 1)           # 1 of 4 = 25 %: not over the guard
        r = _backfill(client, prune_orphans="true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["orphans_found"] == 1 and body["orphans_removed"] == 1
        archive = Path(body["orphans_archive"])
        assert archive.parent == tmp_path / "cody" / "vec-orphans"
        assert archive.name.startswith("cody-") and archive.suffix == ".jsonl"
        [row] = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
        assert row["memory_id"] == ids[0] and row["text"].strip() == "tool 0 is on shelf 0"
        assert row["source_file"].endswith(f"{ids[0]}.json") and row["created_at"]
        assert "category" in row
        store = _vec(client)
        assert not store.has(ids[0]) and store.source_row(ids[0]) is None
        assert store._conn.execute("SELECT COUNT(*) FROM vec_lex WHERE memory_id = ?",
                                   (ids[0],)).fetchone()[0] == 0
        assert all(store.has(m) for m in ids[1:])
        s = _status(client)
        assert s["orphans"] == 0 and s["indexed"] == 3 and s["memory_entries_on_disk"] == 3


def test_dry_run_counts_and_touches_nothing(tmp_path):
    with _client(tmp_path / "cody") as client:
        ids = _world(tmp_path, client, 4, 1)
        body = _backfill(client, prune_orphans="true", dry_run="true").json()
        assert (body["orphans_found"], body["orphans_removed"], body["orphans_archive"]) == (1, 0, None)
        assert _vec(client).has(ids[0])
        assert not (tmp_path / "cody" / "vec-orphans").exists()


def test_over_a_quarter_refuses_and_force_overrides_it(tmp_path):
    with _client(tmp_path / "cody") as client:
        ids = _world(tmp_path, client, 4, 2)           # 2 of 4 = 50 %
        r = _backfill(client, prune_orphans="true")
        assert r.status_code == 409 and "2 of 4" in r.json()["detail"]
        assert all(_vec(client).has(m) for m in ids)
        assert not (tmp_path / "cody" / "vec-orphans").exists()
        r = _backfill(client, prune_orphans="true", force="true")
        assert r.status_code == 200 and r.json()["orphans_removed"] == 2
        assert _status(client)["orphans"] == 0


@pytest.mark.parametrize("folder", ["empty", "missing"])
def test_an_empty_or_missing_folder_refuses_even_with_force(tmp_path, folder):
    with _client(tmp_path / "cody") as client:
        ids = _world(tmp_path, client, 4, 4)
        if folder == "missing":
            (tmp_path / "cody" / "memory").rmdir()
        r = _backfill(client, prune_orphans="true", force="true")
        assert r.status_code == 409 and "reindex question" in r.json()["detail"]
        assert all(_vec(client).has(m) for m in ids)
        assert not (tmp_path / "cody" / "vec-orphans").exists()


def test_a_moved_data_dir_prunes_nothing(tmp_path):
    """The NEGATIVE: every stored path points at the old home; the rebase
    finds each record by name, so nothing is an orphan and nothing goes."""
    old, new = tmp_path / "old" / "cody", tmp_path / "new" / "cody"
    with _client(old) as client:
        ids = [_save(client, "the lathe is in bay two"), _save(client, "the mill is in bay three")]
    shutil.move(str(old), str(new))
    with _client(new) as client:
        body = _backfill(client, prune_orphans="true", force="true").json()
        assert body["orphans_found"] == 0 and body["orphans_removed"] == 0
        assert body["orphans_archive"] is None
        assert all(_vec(client).has(m) for m in ids)
        assert not (new / "vec-orphans").exists()


# ── the function itself ──

def _store_with(tmp_path, names_on_disk, names_indexed) -> tuple[VecStore, Path]:
    memory = tmp_path / "memory"
    memory.mkdir()
    for name in names_on_disk:
        (memory / f"{name}.json").write_text("{}")
    store = VecStore(tmp_path / "vec.sqlite")
    for name in names_indexed:
        store.upsert(name, f"text of {name}", list(VEC),
                     source_file=(memory / f"{name}.json").as_posix())
    return store, memory


def test_an_id_saved_again_after_the_count_keeps_its_row(tmp_path):
    """count_orphans runs off the loop; a save can land before the prune. The
    prune re-checks each id, so the counted-but-now-present one stays."""
    store, memory = _store_with(tmp_path, ["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e", "f"])
    try:
        stats = prune_orphans(store, memory, ["e", "f", "nope"], tmp_path / "arch", "cody")
        assert (stats["orphans_found"], stats["orphans_removed"]) == (1, 1)
        assert store.has("e") and not store.has("f")
    finally:
        store.close()


def test_an_archive_that_cannot_be_written_deletes_nothing(tmp_path):
    store, memory = _store_with(tmp_path, ["a", "b", "c", "d"], ["a", "b", "c", "d", "gone"])
    (tmp_path / "arch").write_text("a file where the archive folder should be")
    try:
        with pytest.raises(OSError):
            prune_orphans(store, memory, ["gone"], tmp_path / "arch", "cody")
        assert store.has("gone")
    finally:
        store.close()


def test_the_refusal_is_raised_before_anything_is_touched(tmp_path):
    store, memory = _store_with(tmp_path, ["a"], ["a", "x", "y"])
    try:
        with pytest.raises(OrphanPruneRefused):
            prune_orphans(store, memory, ["x", "y"], tmp_path / "arch", "cody")
        assert store.has("x") and store.has("y") and not (tmp_path / "arch").exists()
    finally:
        store.close()


def test_a_folder_with_only_stray_files_still_refuses(tmp_path):
    """A desktop.ini or a leftover *.json.tmp is not a record: the
    always-refuse guard must not be disarmed by it (reviewer, 14 C)."""
    store, memory = _store_with(tmp_path, [], ["x", "y"])
    (memory / "desktop.ini").write_text("[.ShellClassInfo]")
    (memory / "x.json.tmp").write_text("{}")
    try:
        with pytest.raises(OrphanPruneRefused, match="holds no record"):
            prune_orphans(store, memory, ["x", "y"], tmp_path / "arch", "cody", force=True)
        assert store.has("x") and store.has("y")
    finally:
        store.close()


def test_a_big_prune_clears_every_table_across_chunks(tmp_path):
    """1,200 orphans span three 500-id chunks of the one delete transaction."""
    gone = [f"g{i:04d}" for i in range(1200)]
    store, memory = _store_with(tmp_path, ["keep"], ["keep"] + gone)
    try:
        stats = prune_orphans(store, memory, gone, tmp_path / "arch", "cody", force=True)
        assert stats["orphans_removed"] == 1200
        archived = (Path(stats["orphans_archive"]).read_text(encoding="utf-8").splitlines())
        assert len(archived) == 1200
        for table in ("vec_sources", "vec_lex", "vec_embeddings"):
            assert store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert store.has("keep")
    finally:
        store.close()


def test_the_recall_warning_names_the_prune(tmp_path, caplog):
    with _client(tmp_path / "cody") as client:
        _world(tmp_path, client, 2, 1)
        with caplog.at_level(logging.WARNING):
            r = client.post("/context", json={"prompt": "tool 0 shelf", "agent_id": "cody"})
        assert r.status_code == 200, r.text
    warned = [m for m in caplog.messages if "no record on disk" in m]
    assert warned and all("prune_orphans=true" in m for m in warned)


# ── note_2: analyst and rulekeeper use the dedup gate's rule ──

class _Reasoner:
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return json.dumps([{"category": "decision", "confidence": "high",
                            "summary": "Chose Hetzner over a shared self-host box.",
                            "key_facts": []}])


class _Embedder:
    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)


def _seed_log(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "log1.json").write_text(json.dumps({
        "id": "log1", "session_id": "cc-jsonl-log1", "summary": "we chose Hetzner again",
        "key_facts": [], "category": "session_log", "source": "tool",
        "created_at": time.time(), "timestamp": "2026-06-10T12:00:00"}))


def test_analyst_an_orphan_does_not_swallow_a_new_note(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir)
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("gone", "Chose Hetzner over a shared self-host box.", list(VEC),
                 source_file=(memory_dir / "gone.json").as_posix(), created_at=time.time())
    try:
        stats = asyncio.run(analyze_tenant("cc", memory_dir, store, _Reasoner(), _Embedder(),
                                           config=AnalysisConfig()))
    finally:
        store.close()
    assert stats["notes_deduped"] == 0 and stats["notes_saved"] == 1


def test_analyst_a_moved_twin_still_dedups_and_its_demotion_holds(tmp_path):
    memory_dir = tmp_path / "memory"
    _seed_log(memory_dir)
    (memory_dir / "twin.json").write_text(json.dumps({"id": "twin", "summary": "x"}))
    store = VecStore(tmp_path / "vec.sqlite")
    store.upsert("twin", "Chose Hetzner over a shared self-host box.", list(VEC),
                 source_file="/old/home/memory/twin.json", created_at=time.time())
    try:
        stats = asyncio.run(analyze_tenant("cc", memory_dir, store, _Reasoner(), _Embedder(),
                                           config=AnalysisConfig()))
        assert stats["notes_deduped"] == 1                 # found by name: a live twin
        # Demote the twin at its moved home: the stored path alone never sees it.
        _seed_log(memory_dir)
        (memory_dir / "twin.json").write_text(json.dumps({"id": "twin", "superseded_by": "x"}))
        stats = asyncio.run(analyze_tenant("cc", memory_dir, store, _Reasoner(), _Embedder(),
                                           config=AnalysisConfig()))
        assert stats["notes_deduped"] == 0
    finally:
        store.close()


def _rulekeeper_world(tmp_path, twin_source: str, twin_record: dict | None) -> dict:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    text = "the nozzle is clogged and printing badly on the big printer"
    vec = [0.0] * EMBED_DIM
    vec[0] = 1.0
    near = [0.0] * EMBED_DIM
    near[0], near[1] = 0.99, 0.141
    store = VecStore(tmp_path / "vec_index.sqlite")
    try:
        new = memory_dir / "new.json"
        new.write_text(json.dumps({"id": "new", "summary": text, "key_facts": [],
                                   "created_at": time.time()}))
        store.upsert("new", text, vec, source_file=new.as_posix(), created_at=time.time())
        if twin_record is not None:
            (memory_dir / "twin.json").write_text(json.dumps(
                {"id": "twin", "summary": text, "key_facts": [], "created_at": 0, **twin_record}))
        store.upsert("twin", text, vec, source_file=twin_source, created_at=time.time())
        live = memory_dir / "live.json"
        live.write_text(json.dumps({"id": "live", "summary": text, "key_facts": [],
                                    "created_at": 0}))
        store.upsert("live", text, near, source_file=live.as_posix(), created_at=time.time())
        return scan_tenant(memory_dir, store, top_k=1)
    finally:
        store.close()


def _pairs(result) -> list[tuple[str, str]]:
    return [(p["new_id"], p["existing_id"]) for p in result["pairs"]]


def test_rulekeeper_skips_an_orphan_hit(tmp_path):
    result = _rulekeeper_world(tmp_path, (tmp_path / "memory" / "twin.json").as_posix(), None)
    assert ("new", "twin") not in _pairs(result) and ("new", "live") in _pairs(result)


def test_rulekeeper_finds_a_moved_twin_and_honours_its_demotion(tmp_path):
    moved = _rulekeeper_world(tmp_path / "a", "/old/home/memory/twin.json", {})
    assert ("new", "twin") in _pairs(moved)
    demoted = _rulekeeper_world(tmp_path / "b", "/old/home/memory/twin.json",
                                {"superseded_by": "twin:by-cc"})
    assert ("new", "twin") not in _pairs(demoted) and ("new", "live") in _pairs(demoted)
