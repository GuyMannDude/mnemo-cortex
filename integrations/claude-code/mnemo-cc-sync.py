#!/usr/bin/env python3
"""
mnemo-cc-sync — push Claude Code session activity to Mnemo Cortex.

This is the modern session-watcher path. It reads Claude Code's JSONL session
files and POSTs structured memories to Mnemo Cortex's /writeback endpoint, so
the memories are immediately recallable by other agents (Opie, Rocky, etc.)
without waiting for an overnight summarization pass.

Replaces the legacy `mnemo-watcher-cc.sh` which wrote raw messages to a
local SQLite that the central Mnemo did not read from.

Configuration (all via env vars, all optional):
    MNEMO_URL              Mnemo Cortex base URL (default: http://localhost:50001)
    MNEMO_AGENT_ID         Agent ID for writebacks (default: cc)
    MNEMO_CC_SESSIONS_DIR  Where Claude Code stores .jsonl session files
                           (default: ~/.claude/projects)
    MNEMO_CC_OFFSET_FILE   Sync offset state file
                           (default: ~/.mnemo-cc/cc-sync.offset.json)

Run modes:
    python3 mnemo-cc-sync.py            # batched: post when >=6 new msgs
    python3 mnemo-cc-sync.py --force    # force-flush regardless of count

Use the companion `mnemo-cc-sync-loop.sh` for periodic invocation
under systemd, or invoke from a cron / scheduler of your choice.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MNEMO_URL = os.environ.get("MNEMO_URL", "http://localhost:50001").rstrip("/")
AGENT_ID = os.environ.get("MNEMO_AGENT_ID", "cc")
SESSIONS_DIR = Path(os.environ.get(
    "MNEMO_CC_SESSIONS_DIR",
    str(Path.home() / ".claude/projects"),
))
OFFSET_FILE = Path(os.environ.get(
    "MNEMO_CC_OFFSET_FILE",
    str(Path.home() / ".mnemo-cc/cc-sync.offset.json"),
))

# One-time migration from the pre-rename default path. The script used to be
# named mnemo-cc-artforge-sync.py and wrote its offset file to
# ~/.mnemo-cc/cc-artforge-sync.offset.json. If we find the old file and the
# user hasn't overridden MNEMO_CC_OFFSET_FILE, move it to the new default
# so existing installs don't reprocess their entire JSONL backlog.
_LEGACY_OFFSET = Path.home() / ".mnemo-cc/cc-artforge-sync.offset.json"
if (
    not os.environ.get("MNEMO_CC_OFFSET_FILE")
    and not OFFSET_FILE.exists()
    and _LEGACY_OFFSET.exists()
):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LEGACY_OFFSET.rename(OFFSET_FILE)

# Batching policy
MIN_TURNS_PER_BATCH = 6
MAX_TURNS_PER_BATCH = 20
SUMMARY_MAX_CHARS = 4000


def get_latest_session_jsonl() -> Path | None:
    """Recursively find the most recently modified .jsonl file under SESSIONS_DIR."""
    if not SESSIONS_DIR.exists():
        return None
    files = sorted(SESSIONS_DIR.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_state() -> dict:
    if not OFFSET_FILE.exists():
        return {}
    try:
        return json.loads(OFFSET_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(OFFSET_FILE)


def extract_text(content) -> str:
    """Flatten Claude Code message content into plain text. Skips thinking parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t == "text":
                pieces.append(part.get("text", ""))
            elif t == "tool_use":
                name = part.get("name", "?")
                pieces.append(f"[tool: {name}]")
            elif t == "tool_result":
                pieces.append("[tool_result]")
        return "\n".join(p for p in pieces if p)
    return ""


def parse_new_messages(jsonl_path: Path, byte_offset: int) -> tuple[list, int]:
    """Returns (messages, new_byte_offset)."""
    messages = []
    with jsonl_path.open("r") as fh:
        fh.seek(byte_offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") not in ("user", "assistant", "message"):
                continue
            msg = payload.get("message") or {}
            role = msg.get("role")
            if not role:
                continue
            content = extract_text(msg.get("content", ""))
            if not content.strip():
                continue
            messages.append({
                "role": role,
                "content": content,
                "timestamp": payload.get("timestamp", ""),
            })
        new_offset = fh.tell()
    return messages, new_offset


def build_summary(messages: list, session_id: str) -> tuple[str, list]:
    """Build a structured summary + key facts from a batch of messages."""
    parts = [
        f"Claude Code session activity (auto-sync from JSONL, session={session_id[:8]}).",
        f"{len(messages)} new message(s) since last sync.",
        "",
        "Turns:",
    ]
    used_chars = sum(len(p) for p in parts)

    for m in messages[-MAX_TURNS_PER_BATCH:]:
        role = m["role"]
        content = m["content"]
        snippet = content[:300] + ("…" if len(content) > 300 else "")
        line = f"- [{role}] {snippet}"
        if used_chars + len(line) > SUMMARY_MAX_CHARS:
            parts.append(f"... ({len(messages) - len(parts) + 4} more turns truncated)")
            break
        parts.append(line)
        used_chars += len(line)

    summary = "\n".join(parts)

    # Surface tool invocations as recall-friendly key facts
    key_facts = []
    for m in messages:
        if m["role"] == "assistant" and "[tool:" in m["content"]:
            tools = [
                line.split("[tool:")[1].split("]")[0].strip()
                for line in m["content"].split("\n")
                if "[tool:" in line
            ]
            for t in tools:
                fact = f"{AGENT_ID} invoked tool: {t}"
                if fact not in key_facts:
                    key_facts.append(fact)

    if not key_facts:
        key_facts = [f"{AGENT_ID} session {session_id[:8]} activity sync — no tool invocations in this batch"]

    return summary[:SUMMARY_MAX_CHARS], key_facts[:10]


def post_to_mnemo(session_id: str, summary: str, key_facts: list) -> dict:
    payload = {
        "session_id": f"{AGENT_ID}-jsonl-{session_id[:12]}",
        "summary": summary,
        "key_facts": key_facts,
        "projects_referenced": [],
        "decisions_made": [],
        "agent_id": AGENT_ID,
    }
    req = urllib.request.Request(
        f"{MNEMO_URL}/writeback",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main(force: bool = False) -> int:
    jsonl = get_latest_session_jsonl()
    if not jsonl:
        print(f"[cc-sync] no session jsonl found under {SESSIONS_DIR}", file=sys.stderr)
        return 0

    session_id = jsonl.stem
    state = load_state()
    last_session = state.get("session_id")
    last_offset = state.get("byte_offset", 0)

    if last_session != session_id:
        last_offset = 0  # New session — start from the top

    messages, new_offset = parse_new_messages(jsonl, last_offset)

    if not messages:
        return 0

    if not force and len(messages) < MIN_TURNS_PER_BATCH:
        return 0  # Defer — wait for more activity

    summary, key_facts = build_summary(messages, session_id)

    try:
        result = post_to_mnemo(session_id, summary, key_facts)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[cc-sync] POST to {MNEMO_URL}/writeback failed: {e}", file=sys.stderr)
        return 1  # Don't update offset — try again next tick

    state.update({
        "session_id": session_id,
        "byte_offset": new_offset,
        "last_post_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "last_memory_id": result.get("memory_id", ""),
    })
    save_state(state)

    print(f"[cc-sync] posted {len(messages)} msgs → memory_id={result.get('memory_id', '?')}")
    return 0


if __name__ == "__main__":
    force = "--force" in sys.argv
    sys.exit(main(force=force))
