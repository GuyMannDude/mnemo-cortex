from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from .common import normalize_whitespace

ReturnMode = Literal["snippet", "verbatim"]


@dataclass(slots=True)
class SearchHit:
    kind: str
    source_id: int
    score: float
    snippet: str


def get_conversation_id(conn: sqlite3.Connection, *, agent_id: str, session_id: str) -> int | None:
    row = conn.execute(
        "SELECT conversation_id FROM conversations WHERE agent_id=? AND session_id=?",
        (agent_id, session_id),
    ).fetchone()
    return int(row["conversation_id"]) if row else None


def search_messages(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    query: str,
    limit: int = 10,
) -> list[SearchHit]:
    rows = conn.execute(
        """
        SELECT m.message_id, snippet(messages_fts, 0, '[', ']', ' … ', 18) AS snippet,
               bm25(messages_fts) AS score
        FROM messages_fts
        JOIN messages m ON m.message_id = messages_fts.rowid
        WHERE messages_fts MATCH ? AND m.conversation_id=?
        ORDER BY score
        LIMIT ?
        """,
        (query, conversation_id, limit),
    ).fetchall()
    return [SearchHit("message", int(r["message_id"]), float(r["score"]), r["snippet"]) for r in rows]


def search_summaries(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    query: str,
    limit: int = 10,
) -> list[SearchHit]:
    rows = conn.execute(
        """
        SELECT s.summary_id, snippet(summaries_fts, 1, '[', ']', ' … ', 18) AS snippet,
               bm25(summaries_fts) AS score
        FROM summaries_fts
        JOIN summaries s ON s.summary_id = summaries_fts.summary_id
        WHERE summaries_fts MATCH ? AND s.conversation_id=?
        ORDER BY score
        LIMIT ?
        """,
        (query, conversation_id, limit),
    ).fetchall()
    return [SearchHit("summary", int(r["summary_id"]), float(r["score"]), r["snippet"]) for r in rows]


def _summary_sources(conn: sqlite3.Connection, summary_id: int, conversation_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT ss.source_summary_id
        FROM summary_sources ss
        JOIN summaries s ON s.summary_id = ss.summary_id
        WHERE ss.summary_id=? AND s.conversation_id=?
        ORDER BY ss.ordinal ASC
        """,
        (summary_id, conversation_id),
    ).fetchall()
    return [int(r["source_summary_id"]) for r in rows]


def _summary_messages(conn: sqlite3.Connection, summary_id: int, conversation_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT m.message_id, m.role, m.content, m.seq
        FROM summary_messages sm
        JOIN messages m ON m.message_id = sm.message_id
        WHERE sm.summary_id=? AND m.conversation_id=?
        ORDER BY sm.ordinal ASC
        """,
        (summary_id, conversation_id),
    ).fetchall()


def expand_summary(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    summary_id: int,
    include_messages: bool = True,
    return_mode: ReturnMode = "snippet",
    max_depth: int = 8,
) -> dict:
    root = conn.execute(
        "SELECT summary_id, kind, depth, content FROM summaries WHERE summary_id=? AND conversation_id=?",
        (summary_id, conversation_id),
    ).fetchone()
    if not root:
        raise ValueError(f"Summary {summary_id} not found in conversation {conversation_id}")

    def recurse(current_summary_id: int, depth_left: int) -> dict:
        row = conn.execute(
            "SELECT summary_id, kind, depth, content FROM summaries WHERE summary_id=? AND conversation_id=?",
            (current_summary_id, conversation_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Summary {current_summary_id} not found in conversation {conversation_id}")

        node = {
            "summary_id": int(row["summary_id"]),
            "kind": row["kind"],
            "depth": int(row["depth"]),
            "content": row["content"] if return_mode == "verbatim" else normalize_whitespace(row["content"])[:300],
            "source_summaries": [],
            "source_messages": [],
        }
        if include_messages:
            for msg in _summary_messages(conn, current_summary_id, conversation_id):
                text = msg["content"] if return_mode == "verbatim" else normalize_whitespace(msg["content"])[:220]
                node["source_messages"].append(
                    {
                        "message_id": int(msg["message_id"]),
                        "role": msg["role"],
                        "seq": int(msg["seq"]),
                        "content": text,
                    }
                )

        if depth_left <= 0:
            return node
        for source_id in _summary_sources(conn, current_summary_id, conversation_id):
            node["source_summaries"].append(recurse(source_id, depth_left - 1))
        return node

    return recurse(summary_id, max_depth)
