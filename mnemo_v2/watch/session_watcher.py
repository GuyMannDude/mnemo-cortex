from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from mnemo_v2.db.migrations import ensure_database
from mnemo_v2.store.compaction import CompactionConfig, compact_if_needed
from mnemo_v2.store.ingest import MessageInput, ingest_messages


def _extract_text(content: Any) -> str:
    """Flatten OpenClaw v3 content (array of parts) into a plain string.

    Handles:
      - str: returned as-is
      - list[{"type": "text", "text": "..."}]: text parts joined
      - list[{"type": "thinking", ...}]: skipped (internal reasoning)
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                pieces.append(part.get("text", ""))
        return "\n".join(pieces)
    return str(content)


class SessionWatcher:
    def __init__(self, db_path: str | Path, session_file: str | Path, checkpoint_file: str | Path):
        self.conn = ensure_database(db_path)
        self.session_file = Path(session_file)
        self.checkpoint_file = Path(checkpoint_file)
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_offset(self) -> int:
        if not self.checkpoint_file.exists():
            return 0
        return int(self.checkpoint_file.read_text(encoding="utf-8").strip() or "0")

    def _save_offset(self, offset: int) -> None:
        self.checkpoint_file.write_text(str(offset), encoding="utf-8")

    def poll_once(self, agent_id: str, session_id: str) -> int:
        if not self.session_file.exists():
            return 0
        offset = self._load_offset()
        processed = 0
        with self.session_file.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)

                # Only process message events — skip session headers,
                # model_change, thinking_level_change, custom, etc.
                if payload.get("type") != "message":
                    continue

                # v3 format: role/content are nested inside payload["message"]
                msg = payload.get("message")
                if not msg:
                    continue

                role = msg.get("role")
                if not role:
                    continue
                # Normalize OpenClaw v3 roles to standard schema roles
                role = {"toolResult": "tool"}.get(role, role)

                # Content is an array of parts in v3, flatten to string
                content = _extract_text(msg.get("content", ""))
                if not content.strip():
                    continue

                processed += 1
                message = MessageInput(
                    role=role,
                    content=content,
                    parts=None,
                )
                result = ingest_messages(
                    self.conn,
                    agent_id=agent_id,
                    session_id=session_id,
                    messages=[message],
                )
                if result["status"] == "ok":
                    compact_if_needed(self.conn, result["conversation_id"], CompactionConfig())
            self._save_offset(fh.tell())
        return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Tail a JSONL session file into Mnemo v2")
    parser.add_argument("session_file")
    parser.add_argument("--db", default=str(Path.home() / ".mnemo-v2" / "mnemo.sqlite3"))
    parser.add_argument("--checkpoint", default=str(Path.home() / ".mnemo-v2" / "watcher.offset"))
    parser.add_argument("--agent-id", default="rocky")
    parser.add_argument("--session-id", default="default-session")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    watcher = SessionWatcher(args.db, args.session_file, args.checkpoint)
    while True:
        watcher.poll_once(agent_id=args.agent_id, session_id=args.session_id)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
