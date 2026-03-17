from __future__ import annotations

import argparse
import time
from pathlib import Path

from mnemo_v2.db.migrations import ensure_database
from mnemo_v2.store.assemble import assemble_context, render_context_markdown
from mnemo_v2.store.retrieval import get_conversation_id


class ContextRefresher:
    def __init__(self, db_path: str | Path, output_path: str | Path):
        self.conn = ensure_database(db_path)
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def refresh_once(self, *, agent_id: str, session_id: str, max_items: int = 24) -> bool:
        conversation_id = get_conversation_id(self.conn, agent_id=agent_id, session_id=session_id)
        if conversation_id is None:
            return False
        items = assemble_context(self.conn, conversation_id=conversation_id, max_items=max_items)
        markdown = render_context_markdown(items)
        self.output_path.write_text(markdown, encoding="utf-8")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Write MNEMO-CONTEXT.md on an interval")
    parser.add_argument("--db", default=str(Path.home() / ".mnemo-v2" / "mnemo.sqlite3"))
    parser.add_argument("--output", default=str(Path.cwd() / "MNEMO-CONTEXT.md"))
    parser.add_argument("--agent-id", default="rocky")
    parser.add_argument("--session-id", default="default-session")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--max-items", type=int, default=24)
    args = parser.parse_args()

    refresher = ContextRefresher(args.db, args.output)
    while True:
        refresher.refresh_once(agent_id=args.agent_id, session_id=args.session_id, max_items=args.max_items)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
