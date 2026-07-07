"""Shared filesystem helpers."""
import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """tmp + os.replace so a reader can never see a half-written file and a
    crash mid-write can never destroy the previous contents."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
