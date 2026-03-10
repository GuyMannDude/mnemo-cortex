"""Mnemo Cortex Recall — Entity reflection.

Reads the memory index and generates per-entity summary pages
in the workspace/bank/entities/ directory.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from sqlite3 import Connection


def write_entity_pages(conn: Connection, workspace: Path, *, max_items: int = 10) -> int:
    """Generate per-entity markdown summary pages from the memory index."""
    out_dir = workspace / "bank" / "entities"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT entity.value AS entity, memories.date, memories.kind, memories.text,
               memories.path, memories.line_start, memories.line_end
        FROM memories
        JOIN json_each(memories.entities_json) AS entity
        ORDER BY entity.value, coalesce(memories.date, '0000-00-00') DESC, memories.id DESC
        """
    )

    grouped: dict[str, list[tuple[str | None, str, str, str, int, int]]] = defaultdict(list)
    for row in rows:
        grouped[row["entity"]].append(
            (row["date"], row["kind"], row["text"], row["path"], row["line_start"], row["line_end"])
        )

    written = 0
    for entity, items in grouped.items():
        path = out_dir / f"{entity.replace(' ', '-')}.md"
        lines = [f"# {entity}", "", "## Recent facts", ""]
        for dt, kind, text, src, ls, le in items[:max_items]:
            stamp = f"[{dt}] " if dt else ""
            lines.append(f"- {stamp}{kind}: {text} _(source: {src}#L{ls}-L{le})_")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written += 1

    return written
