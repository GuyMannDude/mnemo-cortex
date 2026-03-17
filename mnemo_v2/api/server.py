from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mnemo_v2.db.migrations import ensure_database
from mnemo_v2.store.assemble import assemble_context, render_context_markdown
from mnemo_v2.store.common import DEFAULT_CONTEXT_PATH, DEFAULT_DB_PATH
from mnemo_v2.store.compaction import CompactionConfig, compact_if_needed
from mnemo_v2.store.ingest import MessageInput, ingest_messages
from mnemo_v2.store.retrieval import expand_summary, get_conversation_id, search_messages, search_summaries

DB_PATH = Path(__file__).resolve().parents[2] / "mnemo_v2.sqlite3"
conn = ensure_database(DB_PATH)
app = FastAPI(title="Mnemo v2", version="0.1.0")


class MessagePartModel(BaseModel):
    part_type: str = "text"
    content: str | None = None
    data_json: dict | None = None


class MessageModel(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    parts: list[MessagePartModel] | None = None


class IngestRequest(BaseModel):
    agent_id: str
    session_id: str
    title: str | None = None
    messages: list[MessageModel]
    compact: bool = True


class ContextRequest(BaseModel):
    agent_id: str
    session_id: str
    max_items: int = 24
    include_system_messages: bool = True


class ExpandRequest(BaseModel):
    agent_id: str
    session_id: str
    summary_ids: list[int] = Field(default_factory=list)
    query: str | None = None
    include_messages: bool = True
    return_mode: Literal["snippet", "verbatim"] = "snippet"
    max_depth: int = 8
    limit: int = 5


class RefreshRequest(BaseModel):
    agent_id: str
    session_id: str
    output_path: str | None = None
    max_items: int = 24


class PreflightRequest(BaseModel):
    text: str
    mode: Literal["default", "strict", "creative"] = "default"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db_path": str(DB_PATH)}


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    messages = [
        MessageInput(
            role=m.role,
            content=m.content,
            parts=[p.model_dump() for p in m.parts] if m.parts else None,
        )
        for m in req.messages
    ]
    result = ingest_messages(conn, agent_id=req.agent_id, session_id=req.session_id, messages=messages, title=req.title)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["error"])
    if req.compact:
        compact_if_needed(conn, result["conversation_id"], CompactionConfig())
    return result


@app.post("/context")
def context(req: ContextRequest) -> dict:
    conversation_id = get_conversation_id(conn, agent_id=req.agent_id, session_id=req.session_id)
    if conversation_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    items = assemble_context(
        conn,
        conversation_id=conversation_id,
        max_items=req.max_items,
        include_system_messages=req.include_system_messages,
    )
    return {
        "conversation_id": conversation_id,
        "items": [item.__dict__ for item in items],
    }


@app.post("/search")
def search(req: ExpandRequest) -> dict:
    conversation_id = get_conversation_id(conn, agent_id=req.agent_id, session_id=req.session_id)
    if conversation_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not req.query:
        raise HTTPException(status_code=400, detail="query is required for /search")
    return {
        "messages": [hit.__dict__ for hit in search_messages(conn, conversation_id=conversation_id, query=req.query, limit=req.limit)],
        "summaries": [hit.__dict__ for hit in search_summaries(conn, conversation_id=conversation_id, query=req.query, limit=req.limit)],
    }


@app.post("/expand")
def expand(req: ExpandRequest) -> dict:
    conversation_id = get_conversation_id(conn, agent_id=req.agent_id, session_id=req.session_id)
    if conversation_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    summary_ids = list(req.summary_ids)
    if req.query and not summary_ids:
        hits = search_summaries(conn, conversation_id=conversation_id, query=req.query, limit=req.limit)
        summary_ids = [hit.source_id for hit in hits]
    if not summary_ids:
        raise HTTPException(status_code=400, detail="Provide summary_ids or a query that finds summaries")

    return {
        "conversation_id": conversation_id,
        "expanded": [
            expand_summary(
                conn,
                conversation_id=conversation_id,
                summary_id=sid,
                include_messages=req.include_messages,
                return_mode=req.return_mode,
                max_depth=req.max_depth,
            )
            for sid in summary_ids
        ],
    }


@app.post("/refresh")
def refresh(req: RefreshRequest) -> dict:
    conversation_id = get_conversation_id(conn, agent_id=req.agent_id, session_id=req.session_id)
    if conversation_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    items = assemble_context(conn, conversation_id=conversation_id, max_items=req.max_items)
    markdown = render_context_markdown(items)
    output_path = Path(req.output_path) if req.output_path else DEFAULT_CONTEXT_PATH
    output_path.write_text(markdown, encoding="utf-8")
    return {"output_path": str(output_path), "bytes_written": len(markdown.encode("utf-8"))}


@app.post("/preflight")
def preflight(req: PreflightRequest) -> dict:
    text = req.text.strip()
    if not text:
        return {"decision": "BLOCK", "reason": "Empty content."}
    if req.mode == "strict" and len(text) < 20:
        return {"decision": "WARN", "reason": "Very short draft; verify facts before sending."}
    if "TODO" in text.upper():
        return {"decision": "WARN", "reason": "Draft still contains TODO markers."}
    return {"decision": "PASS", "reason": "No obvious problems found by scaffold rules."}
