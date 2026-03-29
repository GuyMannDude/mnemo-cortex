from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .common import dumps, estimate_tokens, normalize_whitespace, renumber_context_ordinals
from .ingest import journal_compaction_message

_SUMMARY_MODEL = os.environ.get("MNEMO_SUMMARY_MODEL", "qwen2.5:32b-instruct")
_SUMMARY_URL = os.environ.get("MNEMO_SUMMARY_URL", "http://localhost:11434/v1/chat/completions")



def deterministic_summary(rows: Iterable[sqlite3.Row], *, max_chars: int = 900) -> str:
    """Truncation-based fallback summarizer. No LLM required."""
    lines: list[str] = []
    for row in rows:
        label = row["role"] if "role" in row.keys() else f"summary-d{row['depth']}"
        content = normalize_whitespace(row["content"])
        lines.append(f"- {label}: {content[:180]}")
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined = joined[: max_chars - 3].rstrip() + "..."
    return joined


def _build_prompt(rows: list[sqlite3.Row], kind: str) -> str:
    """Build a summarization prompt from source rows."""
    parts: list[str] = []
    for row in rows:
        label = row["role"] if "role" in row.keys() else f"summary-d{row['depth']}"
        content = normalize_whitespace(row["content"])
        parts.append(f"[{label}]: {content[:500]}")
    transcript = "\n\n".join(parts)

    if kind == "condensed":
        instruction = (
            "You are summarizing multiple summaries into a higher-level summary. "
            "Preserve key facts, decisions, and action items. "
            "Compress aggressively but never drop named entities, URLs, file paths, or numbers."
        )
    else:
        instruction = (
            "You are summarizing a segment of an AI agent conversation. "
            "Preserve key facts, decisions, tool results, and action items. "
            "Compress aggressively but never drop named entities, URLs, file paths, or numbers."
        )

    return (
        f"{instruction}\n\n"
        f"Conversation segment ({len(rows)} messages):\n\n"
        f"{transcript}\n\n"
        "Write a concise summary (3-8 sentences). No preamble. Start directly with the content."
    )


def llm_summary(rows: list[sqlite3.Row], *, kind: str = "leaf") -> str:
    """Summarize using local Ollama. Falls back to deterministic_summary on failure."""
    prompt = _build_prompt(rows, kind)
    payload = json.dumps({
        "model": _SUMMARY_MODEL,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }).encode("utf-8")

    req = urllib.request.Request(
        _SUMMARY_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"].strip()
        if content:
            return content
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, TimeoutError) as exc:
        import sys
        print(f"[mnemo-v2] LLM summary failed ({exc}), using fallback", file=sys.stderr)

    return deterministic_summary(rows)


@dataclass(slots=True)
class CompactionConfig:
    fresh_tail_messages: int = 6
    leaf_chunk_size: int = 8
    condensed_chunk_size: int = 4
    threshold_items: int = 24
    use_llm: bool = True


def _summarize(rows: list[sqlite3.Row], *, kind: str, use_llm: bool) -> str:
    if use_llm:
        return llm_summary(rows, kind=kind)
    return deterministic_summary(rows)


def _insert_summary(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    kind: str,
    depth: int,
    content: str,
    earliest_seq: int | None,
    latest_seq: int | None,
    descendant_count: int,
    descendant_token_count: int,
    source_message_count: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO summaries(
          conversation_id, kind, depth, content, token_count,
          earliest_seq, latest_seq, descendant_count,
          descendant_token_count, source_message_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            kind,
            depth,
            content,
            estimate_tokens(content),
            earliest_seq,
            latest_seq,
            descendant_count,
            descendant_token_count,
            source_message_count,
        ),
    )
    summary_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO summaries_fts(summary_id, content) VALUES (?, ?)",
        (summary_id, content),
    )
    return summary_id


def _replace_context_span_with_summary(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    start_ordinal: int,
    end_ordinal: int,
    summary_id: int,
) -> None:
    conn.execute(
        "DELETE FROM context_items WHERE conversation_id=? AND ordinal BETWEEN ? AND ?",
        (conversation_id, start_ordinal, end_ordinal),
    )
    conn.execute(
        "INSERT INTO context_items(conversation_id, ordinal, item_type, summary_id) VALUES (?, ?, 'summary', ?)",
        (conversation_id, start_ordinal, summary_id),
    )
    renumber_context_ordinals(conn, conversation_id)


def leaf_pass(conn: sqlite3.Connection, conversation_id: int, config: CompactionConfig) -> int | None:
    rows = conn.execute(
        """
        SELECT ci.ordinal, m.message_id, m.seq, m.role, m.content, m.token_count
        FROM context_items ci
        JOIN messages m ON m.message_id = ci.message_id
        WHERE ci.conversation_id=? AND ci.item_type='message'
        ORDER BY ci.ordinal ASC
        """,
        (conversation_id,),
    ).fetchall()
    if len(rows) <= config.fresh_tail_messages:
        return None

    candidate = rows[: max(0, len(rows) - config.fresh_tail_messages)]
    if len(candidate) < config.leaf_chunk_size:
        return None

    chunk = candidate[: config.leaf_chunk_size]
    content = _summarize(chunk, kind="leaf", use_llm=config.use_llm)
    summary_id = _insert_summary(
        conn,
        conversation_id=conversation_id,
        kind="leaf",
        depth=0,
        content=content,
        earliest_seq=min(r["seq"] for r in chunk),
        latest_seq=max(r["seq"] for r in chunk),
        descendant_count=len(chunk),
        descendant_token_count=sum(int(r["token_count"]) for r in chunk),
        source_message_count=len(chunk),
    )
    for idx, row in enumerate(chunk, start=1):
        conn.execute(
            "INSERT INTO summary_messages(summary_id, message_id, ordinal) VALUES (?, ?, ?)",
            (summary_id, row["message_id"], idx),
        )

    _replace_context_span_with_summary(
        conn,
        conversation_id=conversation_id,
        start_ordinal=min(r["ordinal"] for r in chunk),
        end_ordinal=max(r["ordinal"] for r in chunk),
        summary_id=summary_id,
    )
    conn.execute(
        "INSERT INTO compaction_events(conversation_id, trigger_type, phase, source_count, summary_id, details_json) VALUES (?, 'leaf', 'leaf', ?, ?, ?)",
        (conversation_id, len(chunk), summary_id, dumps({"summary_id": summary_id, "message_ids": [r['message_id'] for r in chunk]})),
    )
    journal_compaction_message(
        conn,
        conversation_id=conversation_id,
        phase="leaf",
        trigger_type="leaf",
        details={"summary_id": summary_id, "source_count": len(chunk)},
    )
    conn.commit()
    return summary_id


def condensed_pass(conn: sqlite3.Connection, conversation_id: int, config: CompactionConfig) -> int | None:
    rows = conn.execute(
        """
        SELECT ci.ordinal, s.summary_id, s.depth, s.content, s.token_count, s.earliest_seq, s.latest_seq,
               s.descendant_count, s.descendant_token_count, s.source_message_count
        FROM context_items ci
        JOIN summaries s ON s.summary_id = ci.summary_id
        WHERE ci.conversation_id=? AND ci.item_type='summary'
        ORDER BY ci.ordinal ASC
        """,
        (conversation_id,),
    ).fetchall()
    if len(rows) < config.condensed_chunk_size:
        return None

    target_depth = min(int(r["depth"]) for r in rows)
    same_depth = [r for r in rows if int(r["depth"]) == target_depth]
    if len(same_depth) < config.condensed_chunk_size:
        return None

    chunk = same_depth[: config.condensed_chunk_size]
    content = _summarize(chunk, kind="condensed", use_llm=config.use_llm)
    summary_id = _insert_summary(
        conn,
        conversation_id=conversation_id,
        kind="condensed",
        depth=target_depth + 1,
        content=content,
        earliest_seq=min(int(r["earliest_seq"] or 0) for r in chunk) or None,
        latest_seq=max(int(r["latest_seq"] or 0) for r in chunk) or None,
        descendant_count=sum(int(r["descendant_count"]) for r in chunk),
        descendant_token_count=sum(int(r["descendant_token_count"]) for r in chunk),
        source_message_count=sum(int(r["source_message_count"]) for r in chunk),
    )
    for idx, row in enumerate(chunk, start=1):
        conn.execute(
            "INSERT INTO summary_sources(summary_id, source_summary_id, ordinal) VALUES (?, ?, ?)",
            (summary_id, int(row["summary_id"]), idx),
        )

    _replace_context_span_with_summary(
        conn,
        conversation_id=conversation_id,
        start_ordinal=min(r["ordinal"] for r in chunk),
        end_ordinal=max(r["ordinal"] for r in chunk),
        summary_id=summary_id,
    )
    conn.execute(
        "INSERT INTO compaction_events(conversation_id, trigger_type, phase, source_count, summary_id, details_json) VALUES (?, 'threshold', 'condensed', ?, ?, ?)",
        (conversation_id, len(chunk), summary_id, dumps({"summary_id": summary_id, "source_summary_ids": [r['summary_id'] for r in chunk]})),
    )
    journal_compaction_message(
        conn,
        conversation_id=conversation_id,
        phase="condensed",
        trigger_type="threshold",
        details={"summary_id": summary_id, "source_count": len(chunk)},
    )
    conn.commit()
    return summary_id


def compact_if_needed(conn: sqlite3.Connection, conversation_id: int, config: CompactionConfig | None = None) -> dict:
    config = config or CompactionConfig()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM context_items WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    count = int(row["n"])
    created: list[int] = []
    if count > config.threshold_items:
        while True:
            sid = leaf_pass(conn, conversation_id, config)
            if sid is None:
                break
            created.append(sid)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM context_items WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if int(row["n"]) <= config.threshold_items:
                break

        while True:
            sid = condensed_pass(conn, conversation_id, config)
            if sid is None:
                break
            created.append(sid)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM context_items WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if int(row["n"]) <= config.threshold_items:
                break

    return {"conversation_id": conversation_id, "created_summary_ids": created}
