from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .common import MessageInput, dumps, estimate_tokens, normalize_whitespace, renumber_context_ordinals


def ensure_conversation(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    session_id: str,
    title: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT conversation_id FROM conversations WHERE agent_id=? AND session_id=?",
        (agent_id, session_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE conversations SET updated_at=CURRENT_TIMESTAMP, title=COALESCE(?, title) WHERE conversation_id=?",
            (title, row["conversation_id"]),
        )
        return int(row["conversation_id"])

    cur = conn.execute(
        "INSERT INTO conversations(agent_id, session_id, title) VALUES (?, ?, ?)",
        (agent_id, session_id, title),
    )
    return int(cur.lastrowid)


def append_raw_tape(conn: sqlite3.Connection, *, agent_id: str, session_id: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO raw_tape(agent_id, session_id, payload_json) VALUES (?, ?, ?)",
        (agent_id, session_id, dumps(payload)),
    )


def build_parts(message: MessageInput) -> list[dict]:
    if message.parts:
        return message.parts
    return [{"part_type": "text", "content": message.content, "data_json": None}]


def next_seq(conn: sqlite3.Connection, conversation_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return int(row["max_seq"]) + 1


def next_context_ordinal(conn: sqlite3.Connection, conversation_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(ordinal), 0) AS max_ordinal FROM context_items WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return int(row["max_ordinal"]) + 1


def ingest_messages(
    conn: sqlite3.Connection,
    *,
    agent_id: str,
    session_id: str,
    messages: Iterable[MessageInput],
    title: str | None = None,
) -> dict:
    payload = {
        "agent_id": agent_id,
        "session_id": session_id,
        "messages": [{"role": message.role, "content": message.content, "parts": message.parts} for message in messages],
    }
    append_raw_tape(conn, agent_id=agent_id, session_id=session_id, payload=payload)
    conversation_id = ensure_conversation(conn, agent_id=agent_id, session_id=session_id, title=title)
    created_ids: list[int] = []
    try:
        seq = next_seq(conn, conversation_id)
        ordinal = next_context_ordinal(conn, conversation_id)
        for message in messages:
            content = normalize_whitespace(message.content)
            cur = conn.execute(
                "INSERT INTO messages(conversation_id, seq, role, content, token_count) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, seq, message.role, content, estimate_tokens(content)),
            )
            message_id = int(cur.lastrowid)
            created_ids.append(message_id)
            conn.execute(
                "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                (message_id, content),
            )
            for idx, part in enumerate(build_parts(message), start=1):
                conn.execute(
                    "INSERT INTO message_parts(message_id, ordinal, part_type, content, data_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        message_id,
                        idx,
                        part.get("part_type", "text"),
                        part.get("content"),
                        json.dumps(part.get("data_json"), ensure_ascii=False) if part.get("data_json") is not None else None,
                    ),
                )
            conn.execute(
                "INSERT INTO context_items(conversation_id, ordinal, item_type, message_id) VALUES (?, ?, 'message', ?)",
                (conversation_id, ordinal, message_id),
            )
            seq += 1
            ordinal += 1

        conn.execute(
            "INSERT INTO ingest_events(conversation_id, session_id, status) VALUES (?, ?, 'ok')",
            (conversation_id, session_id),
        )
        conn.commit()
        return {"conversation_id": conversation_id, "message_ids": created_ids, "status": "ok"}
    except Exception as exc:
        conn.rollback()
        append_raw_tape(conn, agent_id=agent_id, session_id=session_id, payload={"ingest_error": str(exc), **payload})
        conn.execute(
            "INSERT INTO ingest_events(conversation_id, session_id, status, error_text) VALUES (?, ?, 'error', ?)",
            (conversation_id, session_id, str(exc)),
        )
        conn.commit()
        return {"conversation_id": conversation_id, "message_ids": created_ids, "status": "error", "error": str(exc)}


def journal_compaction_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    phase: str,
    trigger_type: str,
    details: dict,
) -> int:
    seq = next_seq(conn, conversation_id)
    ordinal = next_context_ordinal(conn, conversation_id)
    content = f"[compaction:{phase}] {trigger_type} -> {details.get('summary_id')} from {details.get('source_count')} sources"
    cur = conn.execute(
        "INSERT INTO messages(conversation_id, seq, role, content, token_count) VALUES (?, ?, 'system', ?, ?)",
        (conversation_id, seq, content, estimate_tokens(content)),
    )
    message_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
        (message_id, content),
    )
    conn.execute(
        "INSERT INTO message_parts(message_id, ordinal, part_type, content, data_json) VALUES (?, 1, 'compaction', ?, ?)",
        (message_id, content, dumps(details)),
    )
    conn.execute(
        "INSERT INTO context_items(conversation_id, ordinal, item_type, message_id) VALUES (?, ?, 'message', ?)",
        (conversation_id, ordinal, message_id),
    )
    renumber_context_ordinals(conn, conversation_id)
    return message_id
