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
    """Snapshot memory/ + vec_index.sqlite to a timestamped backup dir."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = data_dir / ".migrate-backups" / ts
    dest.mkdir(parents=True, exist_ok=True)
    mem = data_dir / "memory"
    if mem.is_dir():
        shutil.copytree(mem, dest / "memory")
    vec = data_dir / "vec_index.sqlite"
    if vec.exists():
        shutil.copy2(vec, dest / "vec_index.sqlite")
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

    total = len(list(memory_dir.glob("*.json")))
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
        )
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
