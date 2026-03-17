from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".mnemo-v2" / "mnemo.sqlite3"
DEFAULT_CONTEXT_PATH = Path.cwd() / "MNEMO-CONTEXT.md"

WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(slots=True)
class MessageInput:
    role: str
    content: str
    parts: list[dict[str, Any]] | None = None


def utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(WORD_RE.findall(text)) * 1.3)) if text else 0


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def renumber_context_ordinals(conn: sqlite3.Connection, conversation_id: int) -> None:
    rows = conn.execute(
        "SELECT context_item_id FROM context_items WHERE conversation_id=? ORDER BY ordinal, context_item_id",
        (conversation_id,),
    ).fetchall()
    for idx, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE context_items SET ordinal=? WHERE context_item_id=?",
            (idx, row["context_item_id"]),
        )


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())
