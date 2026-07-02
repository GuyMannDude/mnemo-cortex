"""
Mnemo Cortex — migration: one-time reclassification (v4.0)
=========================================================
Backs up a tenant's store, then reclassifies every uncategorized / `unknown` /
routine-log memory with the reasoning LLM so real memories (Tier 1) stop sharing
recall slots with raw session logs (Tier 2). Ships as `mnemo-cortex migrate
reclassify` — any user with a polluted store runs the same command.

Safety model:
  - Rewrites ONLY the JSON `category` field (category is disk-only metadata read
    at recall time) — embeddings / vec_sources are never touched, so there is no
    vector loss and the store stays queryable throughout.
  - Backup-before-write by default (snapshot of memory/ + vec_index.sqlite).
  - `--dry-run` writes nothing and prints the projected before→after spread.
  - Default is DEMOTE (logs move to Tier-2 `session_log`, retained as the archive
    per the two-tier vision). `--purge-noise` deletes ONLY empty/sentinel rows
    (blank summary, auto_capture_flush) — never legitimate session logs.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from agentb.config import load_config, get_agent_data_dir
from agentb.providers import create_resilient_reasoning
from agentb.classify import reclassify_memory_dir

console = Console()


def _backup(data_dir: Path) -> Path:
    """Snapshot memory/ + vec_index.sqlite + trajectories/ to a timestamped backup dir."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = data_dir / ".migrate-backups" / ts
    dest.mkdir(parents=True, exist_ok=True)
    mem = data_dir / "memory"
    if mem.is_dir():
        shutil.copytree(mem, dest / "memory")
    vec = data_dir / "vec_index.sqlite"
    if vec.exists():
        shutil.copy2(vec, dest / "vec_index.sqlite")
    traj = data_dir / "trajectories"
    if traj.is_dir():
        shutil.copytree(traj, dest / "trajectories")
    return dest


def _category_spread(memory_dir: Path) -> dict:
    counts: dict = {}
    for p in memory_dir.glob("*.json"):
        try:
            cat = json.loads(p.read_text()).get("category", "<none>")
        except Exception:
            cat = "<unreadable>"
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def _purge_empty(data_dir: Path) -> int:
    """Delete only genuinely empty/sentinel rows. Never deletes real session logs
    (Tier 2 is the archive). Removes the JSON and its vec row to stay consistent."""
    from agentb.vec import VecStore
    memory_dir = data_dir / "memory"
    vec_path = data_dir / "vec_index.sqlite"
    store = VecStore(vec_path) if vec_path.exists() else None
    removed = 0
    try:
        for p in list(memory_dir.glob("*.json")):
            try:
                entry = json.loads(p.read_text())
            except Exception:
                continue
            summary = (entry.get("summary") or "").strip()
            key_facts = entry.get("key_facts") or []
            sentinel = (not summary) or (
                key_facts and all((f or "").strip().lower() == "auto_capture_flush" for f in key_facts)
                and len(summary) < 40
            )
            if sentinel:
                mid = entry.get("id") or p.stem
                if store is not None:
                    store.delete(mid)
                p.unlink(missing_ok=True)
                removed += 1
    finally:
        if store is not None:
            store.close()
    return removed


async def _run_one(agent_id: str, data_dir: Path, *, dry_run: bool, backup: bool,
                   include_routine: bool, max_input_chars: int, reasoner) -> dict:
    memory_dir = data_dir / "memory"
    if not memory_dir.is_dir():
        console.print(f"  [yellow]{agent_id}: no memory dir at {memory_dir} — skipping[/]")
        return {"agent": agent_id, "skipped_empty": True}

    before = _category_spread(memory_dir)
    backup_path = None
    if backup and not dry_run:
        backup_path = _backup(data_dir)
        console.print(f"  [dim]{agent_id}: backed up → {backup_path}[/]")

    # #468: keep vec_sources.category in step with the JSON as we reclassify, so
    # the search pre-filter column never goes stale. Opened only for real runs.
    from agentb.vec import VecStore
    vec_path = data_dir / "vec_index.sqlite"
    store = VecStore(vec_path) if (vec_path.exists() and not dry_run) else None
    on_reclassified = store.update_category if store is not None else None

    total = len(list(memory_dir.glob("*.json")))
    try:
        with Progress(
            TextColumn("[cyan]" + agent_id + "[/]"), BarColumn(),
            TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
            console=console, transient=True,
        ) as progress:
            task = progress.add_task("reclassify", total=total)
            stats = await reclassify_memory_dir(
                memory_dir, reasoner,
                limit=None, max_input_chars=max_input_chars,
                include_routine=include_routine, dry_run=dry_run, use_breaker=False,
                on_progress=lambda done, tot: progress.update(task, completed=done, total=tot),
                on_reclassified=on_reclassified,
            )
    finally:
        if store is not None:
            store.close()
    stats["agent"] = agent_id
    stats["before"] = before
    stats["backup"] = str(backup_path) if backup_path else None
    return stats


async def run_migration(agent_ids: list[str], *, dry_run: bool, backup: bool,
                        include_routine: bool, purge_noise: bool,
                        config=None, reasoner=None) -> list[dict]:
    config = config or load_config()
    if reasoner is None:
        reasoner = create_resilient_reasoning(config.reasoning)  # own instance — never
    max_input_chars = config.classification.max_input_chars       # touches the live breaker
    results = []
    for agent_id in agent_ids:
        data_dir = get_agent_data_dir(config, agent_id)
        res = await _run_one(
            agent_id, data_dir, dry_run=dry_run, backup=backup,
            include_routine=include_routine, max_input_chars=max_input_chars,
            reasoner=reasoner,
        )
        if purge_noise and not dry_run and not res.get("skipped_empty"):
            res["purged_empty"] = _purge_empty(data_dir)
        results.append(res)
    return results


def render_results(results: list[dict], dry_run: bool) -> None:
    table = Table(title="Reclassification " + ("(DRY RUN — nothing written)" if dry_run else "complete"))
    table.add_column("agent", style="cyan")
    table.add_column("scanned", justify="right")
    table.add_column("reclassified", justify="right")
    table.add_column("by category")
    table.add_column("llm/regex/noise")
    table.add_column("purged", justify="right")
    for r in results:
        if r.get("skipped_empty"):
            table.add_row(r["agent"], "—", "—", "(empty store)", "—", "—")
            continue
        by_cat = ", ".join(f"{k}:{v}" for k, v in sorted(r.get("by_category", {}).items()))
        bm = r.get("by_method", {})
        methods = f"{bm.get('llm', 0)}/{bm.get('regex', 0)}/{bm.get('noise-heuristic', 0)}"
        table.add_row(
            r["agent"], str(r.get("scanned", 0)), str(r.get("reclassified", 0)),
            by_cat or "—", methods, str(r.get("purged_empty", "—")),
        )
    console.print(table)


def migrate_reclassify(agent_ids: list[str], *, dry_run: bool, backup: bool,
                       include_routine: bool, purge_noise: bool, config=None) -> list[dict]:
    """Sync entrypoint for the CLI."""
    results = asyncio.run(run_migration(
        agent_ids, dry_run=dry_run, backup=backup,
        include_routine=include_routine, purge_noise=purge_noise, config=config,
    ))
    render_results(results, dry_run)
    return results


# ─────────────────────────────────────────────
#  nomic-prefix migration — full store re-embed
# ─────────────────────────────────────────────

class ReindexAbort(RuntimeError):
    """Primary embedder is down mid-reindex — stop the whole run.

    The reindex exists to make the store single-space (document-prefixed
    primary vectors). Falling back to another provider mid-run would silently
    re-create the mixed-space problem, so a persistent primary failure aborts.
    Re-running resumes safely: memory_ids are deterministic and upsert REPLACEs.
    """


async def _embed_or_abort(embed, text: str) -> tuple[list[float], str]:
    """Embed with adaptive truncation; retry transient failures, then abort."""
    from agentb.vec import embed_with_adaptive_truncation
    last_err = None
    for attempt in range(1, 4):
        try:
            return await embed_with_adaptive_truncation(embed, text)
        except Exception as e:
            last_err = e
            if attempt < 3:
                await asyncio.sleep(2.0 * attempt)
    raise ReindexAbort(f"primary embedder failed 3x, aborting reindex: {last_err}")


def _wipe_caches(data_dir: Path) -> int:
    """Delete L1 bundles + the L2 index — they hold OLD-space document
    embeddings that would be cosine-compared against NEW-space queries.
    L3 re-embeds live, so it self-heals. Returns files removed."""
    removed = 0
    l1 = data_dir / "cache" / "l1"
    if l1.is_dir():
        for f in l1.glob("*.json"):
            f.unlink(missing_ok=True)
            removed += 1
    l2_index = data_dir / "cache" / "l2" / "index.json"
    if l2_index.exists():
        l2_index.unlink()
        removed += 1
    return removed


async def _reindex_one(agent_id: str, data_dir: Path, embed, *, dry_run: bool,
                       backup: bool, include_trajectories: bool) -> dict:
    from agentb.vec import VecStore, iter_memory_entries
    from agentb.trajectory import TrajectoryStore, embedding_text

    memory_dir = data_dir / "memory"
    if not memory_dir.is_dir():
        console.print(f"  [yellow]{agent_id}: no memory dir at {memory_dir} — skipping[/]")
        return {"agent": agent_id, "skipped_empty": True}

    entries = list(iter_memory_entries(memory_dir))
    traj_dir = data_dir / "trajectories"
    traj_records: list[dict] = []
    if include_trajectories and traj_dir.is_dir():
        ts = TrajectoryStore(traj_dir)
        try:
            traj_records = list(ts._load(None).values())
        finally:
            ts.close()

    stats = {"agent": agent_id, "memories": len(entries), "trajectories": len(traj_records),
             "reembedded": 0, "traj_reembedded": 0, "truncated": 0, "cache_files_wiped": 0,
             "backup": None}
    if dry_run:
        return stats

    if backup:
        backup_path = _backup(data_dir)
        stats["backup"] = str(backup_path)
        console.print(f"  [dim]{agent_id}: backed up → {backup_path}[/]")

    store = VecStore(data_dir / "vec_index.sqlite")
    try:
        with Progress(
            TextColumn("[cyan]" + agent_id + " memories[/]"), BarColumn(),
            TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
            console=console, transient=True,
        ) as progress:
            task = progress.add_task("reindex", total=len(entries))
            for memory_id, text, path, created_at, category in entries:
                vec, stored_text = await _embed_or_abort(embed, text)
                if len(stored_text) < len(text):
                    stats["truncated"] += 1
                store.upsert(
                    memory_id, stored_text, vec,
                    source_file=path.as_posix(),
                    created_at=created_at,
                    category=category,
                )
                stats["reembedded"] += 1
                progress.update(task, advance=1)
    finally:
        store.close()

    if traj_records:
        ts = TrajectoryStore(traj_dir)
        try:
            with Progress(
                TextColumn("[cyan]" + agent_id + " trajectories[/]"), BarColumn(),
                TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
                console=console, transient=True,
            ) as progress:
                task = progress.add_task("traj-reindex", total=len(traj_records))
                for rec in traj_records:
                    text = embedding_text(
                        rec.get("task_description", ""), rec.get("outcome", ""),
                        rec.get("steps") or [],
                    )
                    if not text or not rec.get("id"):
                        progress.update(task, advance=1)
                        continue
                    vec, stored_text = await _embed_or_abort(embed, text)
                    ts.vec.upsert(
                        rec["id"], stored_text, vec,
                        source_file=ts._jsonl_path(rec.get("task_type", "unknown")).as_posix(),
                        created_at=rec.get("created_at"),
                        category=rec.get("task_type"),
                    )
                    stats["traj_reembedded"] += 1
                    progress.update(task, advance=1)
        finally:
            ts.close()

    stats["cache_files_wiped"] = _wipe_caches(data_dir)
    return stats


async def run_reindex(agent_ids: list[str], *, dry_run: bool, backup: bool,
                      include_trajectories: bool, config=None, embed=None) -> list[dict]:
    config = config or load_config()
    if embed is None and not dry_run:
        from agentb.providers import create_resilient_embedding
        # Force PRIMARY (nomic) — never the resilient wrapper. A fallback embed
        # mid-migration would write a different-space vector, which is the exact
        # corruption this reindex exists to remove.
        embedder = create_resilient_embedding(config.embedding)
        primary = embedder.primary
        if not await primary.health_check():
            raise ReindexAbort(
                f"primary embedder {primary.label} failed health check — "
                "bring it up, then re-run (idempotent)."
            )
        embed = lambda t: primary.embed(t, task_type="document")
    results = []
    for agent_id in agent_ids:
        data_dir = get_agent_data_dir(config, agent_id)
        results.append(await _reindex_one(
            agent_id, data_dir, embed, dry_run=dry_run, backup=backup,
            include_trajectories=include_trajectories,
        ))
    return results


def render_reindex_results(results: list[dict], dry_run: bool) -> None:
    table = Table(title="Reindex " + ("(DRY RUN — nothing written)" if dry_run else "complete"))
    table.add_column("agent", style="cyan")
    table.add_column("memories", justify="right")
    table.add_column("re-embedded", justify="right")
    table.add_column("trajectories", justify="right")
    table.add_column("traj re-embedded", justify="right")
    table.add_column("truncated", justify="right")
    table.add_column("caches wiped", justify="right")
    for r in results:
        if r.get("skipped_empty"):
            table.add_row(r["agent"], "—", "—", "—", "—", "—", "—")
            continue
        table.add_row(
            r["agent"], str(r["memories"]), str(r["reembedded"]),
            str(r["trajectories"]), str(r["traj_reembedded"]),
            str(r["truncated"]), str(r["cache_files_wiped"]),
        )
    console.print(table)


def migrate_reindex(agent_ids: list[str], *, dry_run: bool, backup: bool,
                    include_trajectories: bool, config=None) -> list[dict]:
    """Sync entrypoint for the CLI. Re-embeds every stored vector through the
    PRIMARY embedder with the nomic `search_document: ` prefix (one-time deploy
    step for the task-prefix fix — run with the server STOPPED)."""
    results = asyncio.run(run_reindex(
        agent_ids, dry_run=dry_run, backup=backup,
        include_trajectories=include_trajectories, config=config,
    ))
    render_reindex_results(results, dry_run)
    return results


# ─────────────────────────────────────────────
#  #468 — one-time vec_sources.category backfill
# ─────────────────────────────────────────────

def migrate_vec_backfill(agent_ids: list[str], *, config=None) -> list[dict]:
    """Populate vec_sources.category from disk truth for each agent's store.

    The deploy step for #468: existing stores get a NULL category column after
    the additive ALTER. This walks each indexed memory's JSON and writes its
    category to the column — NO embedding, just metadata, so it's fast and safe
    to run while the server is up (single-row UPDATEs). Idempotent; re-running
    just re-syncs. Backs up the sqlite file first (cheap insurance).
    """
    from agentb.vec import VecStore, backfill_categories
    config = config or load_config()
    results = []
    for agent_id in agent_ids:
        data_dir = get_agent_data_dir(config, agent_id)
        memory_dir = data_dir / "memory"
        vec_path = data_dir / "vec_index.sqlite"
        if not vec_path.exists() or not memory_dir.is_dir():
            console.print(f"  [yellow]{agent_id}: no vec index / memory dir — skipping[/]")
            results.append({"agent": agent_id, "skipped": True})
            continue
        backup_path = _backup(data_dir)
        console.print(f"  [dim]{agent_id}: backed up → {backup_path}[/]")
        store = VecStore(vec_path)
        try:
            stats = backfill_categories(store, memory_dir)
        finally:
            store.close()
        stats["agent"] = agent_id
        results.append(stats)
        console.print(
            f"  [green]{agent_id}[/]: {stats['updated']} rows synced from disk, "
            f"{stats['missing_json']} indexed rows with no JSON"
        )
    return results
