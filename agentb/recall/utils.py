"""Mnemo Cortex Recall — Utility functions."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import re


def parse_since(value: str | None) -> str | None:
    """Parse a --since value like '14d' or '2026-03-01' into an ISO date string."""
    if not value:
        return None
    value = value.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    m = re.fullmatch(r"(\d+)d", value)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    raise ValueError(f"Unsupported --since value: {value!r}")


def relpath(path: Path, start: Path) -> str:
    """Return a relative path string, falling back to absolute if not relative."""
    try:
        return path.relative_to(start).as_posix()
    except ValueError:
        return path.as_posix()
