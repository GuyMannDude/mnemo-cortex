"""Mnemo Cortex Recall — Markdown memory parser.

Discovers markdown files in the workspace and parses structured
memory bullets into MemoryRecord objects.

Supported bullet format:
    - kind [@entity] [c=0.95] The actual memory text here.

Examples:
    - fact @Guy Guy runs an OpenClaw assistant named Rocky.
    - decision @Rocky c=0.9 We chose Gemini 3.1 Pro as the primary model.
    - lesson Never run retry loops without a spending cap.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import MemoryRecord

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
ENTITY_RE = re.compile(r"@([A-Za-z0-9_.-]+)")
BULLET_RE = re.compile(
    r"^\s*-\s*(?P<kind>[A-Za-z_][A-Za-z0-9_-]*)(?:\s*c=(?P<conf>\d+(?:\.\d+)?))?\s+(?P<text>.+?)\s*$"
)


def infer_date(path: Path) -> str | None:
    """Try to extract a date from the file path (e.g., memory/2026-03-09.md)."""
    m = DATE_RE.search(path.as_posix())
    return m.group(1) if m else None


def discover_markdown_files(workspace: Path) -> list[Path]:
    """Find all markdown memory files in standard locations."""
    roots = [
        workspace / "memory.md",
        workspace / "memory",
        workspace / "bank",
        workspace / "MEMORY.md",
    ]
    found: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix.lower() == ".md":
            found.append(root)
        elif root.is_dir():
            found.extend(sorted(root.rglob("*.md")))
    # Deduplicate preserving order
    seen = set()
    unique: list[Path] = []
    for p in found:
        if p not in seen:
            unique.append(p)
            seen.add(p)
    return unique


def parse_markdown_file(path: Path) -> list[MemoryRecord]:
    """Parse a single markdown file into MemoryRecords."""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    file_date = infer_date(path)
    heading: str | None = None
    records: list[MemoryRecord] = []

    for i, line in enumerate(lines, start=1):
        hm = HEADING_RE.match(line)
        if hm:
            heading = hm.group(2).strip()
            continue

        bm = BULLET_RE.match(line)
        if not bm:
            continue

        raw_text = bm.group("text").strip()
        entities = ENTITY_RE.findall(raw_text)
        cleaned = ENTITY_RE.sub(lambda m: m.group(1), raw_text)
        conf = float(bm.group("conf")) if bm.group("conf") else None

        records.append(
            MemoryRecord(
                path=path,
                line_start=i,
                line_end=i,
                kind=bm.group("kind").lower(),
                text=cleaned,
                entities=entities,
                confidence=conf,
                date=file_date,
                title=heading,
            )
        )

    return records


def iter_records(workspace: Path) -> Iterable[MemoryRecord]:
    """Iterate over all memory records in the workspace."""
    for md in discover_markdown_files(workspace):
        yield from parse_markdown_file(md)
