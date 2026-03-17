from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class AssembledItem:
    item_type: str
    role: str
    content: str
    source_id: int
    depth: int | None = None


def assemble_context(
    conn: sqlite3.Connection,
    *,
    conversation_id: int,
    max_items: int = 24,
    include_system_messages: bool = True,
) -> list[AssembledItem]:
    rows = conn.execute(
        """
        SELECT ci.item_type,
               ci.message_id,
               ci.summary_id,
               m.role AS message_role,
               m.content AS message_content,
               s.content AS summary_content,
               s.depth AS summary_depth
        FROM context_items ci
        LEFT JOIN messages m ON m.message_id = ci.message_id
        LEFT JOIN summaries s ON s.summary_id = ci.summary_id
        WHERE ci.conversation_id=?
        ORDER BY ci.ordinal ASC
        LIMIT ?
        """,
        (conversation_id, max_items),
    ).fetchall()

    assembled: list[AssembledItem] = []
    for row in rows:
        if row["item_type"] == "message":
            if not include_system_messages and row["message_role"] == "system":
                continue
            assembled.append(
                AssembledItem(
                    item_type="message",
                    role=row["message_role"],
                    content=row["message_content"],
                    source_id=int(row["message_id"]),
                )
            )
        else:
            assembled.append(
                AssembledItem(
                    item_type="summary",
                    role="system",
                    content=row["summary_content"],
                    source_id=int(row["summary_id"]),
                    depth=int(row["summary_depth"]),
                )
            )
    return assembled


def render_context_markdown(items: list[AssembledItem]) -> str:
    lines = ["# MNEMO CONTEXT", ""]
    for item in items:
        if item.item_type == "summary":
            lines.append(f"## Summary d{item.depth} #{item.source_id}")
            lines.append(item.content)
            lines.append("")
        else:
            lines.append(f"## {item.role.title()} #{item.source_id}")
            lines.append(item.content)
            lines.append("")
    return "\n".join(lines).strip() + "\n"
