"""Mnemo Cortex Recall — Data models for exact-match memory records."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class MemoryRecord:
    path: Path
    line_start: int
    line_end: int
    kind: str
    text: str
    entities: list[str] = field(default_factory=list)
    confidence: float | None = None
    date: str | None = None
    title: str | None = None
