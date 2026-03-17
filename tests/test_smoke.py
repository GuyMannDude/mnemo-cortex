from pathlib import Path

from mnemo_v2.db.migrations import ensure_database
from mnemo_v2.store.compaction import CompactionConfig, compact_if_needed
from mnemo_v2.store.ingest import MessageInput, ingest_messages
from mnemo_v2.store.retrieval import expand_summary, get_conversation_id


def test_ingest_compact_expand(tmp_path: Path) -> None:
    conn = ensure_database(tmp_path / "test.sqlite3")
    msgs = []
    for i in range(12):
        msgs.append(MessageInput(role="user", content=f"User fact {i}"))
        msgs.append(MessageInput(role="assistant", content=f"Assistant fact {i}"))
    result = ingest_messages(conn, agent_id="rocky", session_id="demo", messages=msgs)
    assert result["status"] == "ok"
    cid = get_conversation_id(conn, agent_id="rocky", session_id="demo")
    assert cid is not None
    out = compact_if_needed(conn, cid, CompactionConfig(threshold_items=8, leaf_chunk_size=4, condensed_chunk_size=2, fresh_tail_messages=2))
    assert out["created_summary_ids"]
    expanded = expand_summary(conn, conversation_id=cid, summary_id=out["created_summary_ids"][0], include_messages=True, return_mode="verbatim")
    assert expanded["summary_id"] == out["created_summary_ids"][0]
    assert "source_messages" in expanded
