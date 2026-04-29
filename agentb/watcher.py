#!/usr/bin/env python3
"""
Mnemo Cortex Session Watcher
=============================
Watches OpenClaw session JSONL files in real-time and auto-ingests
every user/assistant exchange to Mnemo Cortex's /ingest endpoint.

Rocky doesn't have to do anything. This reads directly from what
OpenClaw writes to disk. If a session crashes, every exchange up
to that moment is already in Mnemo.

Usage:
    python3 mnemo_watcher.py
    
Or install as a systemd service (see bottom of file).

Environment:
    MNEMO_URL        - Mnemo Cortex server (default: http://localhost:50001)
    MNEMO_AGENT_ID   - Agent ID for tenant isolation (default: rocky)
    MNEMO_AUTH_TOKEN  - API auth token if configured
    OPENCLAW_SESSIONS - Path to OpenClaw sessions dir
                        (default: ~/.openclaw/agents/main/sessions)
"""

import os
import sys
import json
import time
import re
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import httpx

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

MNEMO_URL = os.environ.get("MNEMO_URL", "http://localhost:50001")
AGENT_ID = os.environ.get("MNEMO_AGENT_ID", "rocky")
AUTH_TOKEN = os.environ.get("MNEMO_AUTH_TOKEN", "")

SESSIONS_DIR = Path(os.environ.get(
    "OPENCLAW_SESSIONS",
    Path.home() / ".openclaw" / "agents" / "main" / "sessions"
))

# State file — tracks what we've already ingested
STATE_DIR = Path.home() / ".agentb" / "watcher"
STATE_FILE = STATE_DIR / "positions.json"

POLL_INTERVAL = 2.0  # seconds between checks
MAX_CONTENT_LENGTH = 3000  # truncate long messages

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mnemo-watcher")

# ─────────────────────────────────────────────
#  State Management
# ─────────────────────────────────────────────

def load_positions() -> dict:
    """Load file positions (how far we've read into each session file)."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_positions(positions: dict):
    """Save current file positions."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(positions, indent=2))


# ─────────────────────────────────────────────
#  Message Extraction
# ─────────────────────────────────────────────

def extract_text(content) -> str:
    """Extract plain text from OpenClaw message content array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


def extract_tool_calls(content) -> list[dict]:
    """Extract tool call summaries from assistant message content."""
    calls = []
    if not isinstance(content, list):
        return calls
    for block in content:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            name = block.get("name", "unknown")
            args = block.get("arguments", {})

            # Summarize the call based on tool type
            summary = ""
            if name == "exec" and isinstance(args, dict):
                cmd = args.get("command", "")
                # Truncate long commands but keep the important parts
                summary = cmd[:300] if cmd else ""
            elif isinstance(args, dict):
                summary = json.dumps(args)[:200]
            else:
                summary = str(args)[:200]

            calls.append({
                "id": block.get("id", ""),
                "tool": name,
                "summary": summary,
            })
    return calls


def extract_thinking(content) -> str:
    """Extract a brief summary of thinking blocks from assistant message content."""
    if not isinstance(content, list):
        return ""
    thinking_parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            text = block.get("text", "")
            if text:
                # Take first 200 chars as a thinking summary
                thinking_parts.append(text[:200].strip())
    if not thinking_parts:
        return ""
    # Join multiple thinking blocks, cap total
    return " | ".join(thinking_parts)[:500]


def strip_sender_metadata(text: str) -> str:
    """Remove the 'Sender (untrusted metadata)' wrapper OpenClaw adds to user messages."""
    pattern = r'^Sender \(untrusted metadata\):\s*```json\s*\{[^}]*\}\s*```\s*'
    cleaned = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    return cleaned if cleaned else text


def parse_session_lines(lines: list[str]) -> list[dict]:
    """
    Parse JSONL lines and extract messages with full context.
    Returns user messages, assistant messages (with tool calls + thinking),
    and tool results — all linked together.
    """
    messages = []
    tool_results = {}  # toolCallId -> result summary

    # First pass: collect tool results
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") != "message":
                continue
            msg = entry.get("message", {})
            if msg.get("role") == "toolResult":
                call_id = msg.get("toolCallId", "")
                if call_id:
                    result_text = extract_text(msg.get("content", ""))
                    details = msg.get("details", {})
                    tool_results[call_id] = {
                        "tool": msg.get("toolName", ""),
                        "status": details.get("status", ""),
                        "exit_code": details.get("exitCode"),
                        "output": result_text[:300],  # truncated output
                        "duration_ms": details.get("durationMs"),
                    }
        except json.JSONDecodeError:
            continue

    # Second pass: build messages with metadata
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") != "message":
                continue
            msg = entry.get("message", {})
            role = msg.get("role")

            if role == "user":
                text = extract_text(msg.get("content", ""))
                text = strip_sender_metadata(text)
                if len(text.strip()) < 2:
                    continue
                messages.append({
                    "role": "user",
                    "text": text[:MAX_CONTENT_LENGTH],
                    "timestamp": msg.get("timestamp") or entry.get("timestamp", ""),
                })

            elif role == "assistant":
                content = msg.get("content", [])
                text = extract_text(content)

                # Extract tool calls and match with results
                tool_calls = extract_tool_calls(content)
                actions = []
                for tc in tool_calls:
                    action = {
                        "tool": tc["tool"],
                        "command": tc["summary"],
                    }
                    # Match with result
                    if tc["id"] in tool_results:
                        result = tool_results[tc["id"]]
                        action["status"] = result.get("status", "")
                        action["exit_code"] = result.get("exit_code")
                        action["output"] = result.get("output", "")[:200]
                    actions.append(action)

                # Extract thinking summary
                thinking = extract_thinking(content)

                if len(text.strip()) < 2 and not actions:
                    continue

                messages.append({
                    "role": "assistant",
                    "text": text[:MAX_CONTENT_LENGTH],
                    "timestamp": msg.get("timestamp") or entry.get("timestamp", ""),
                    "actions": actions if actions else None,
                    "thinking": thinking if thinking else None,
                })

        except json.JSONDecodeError:
            continue

    return messages


def pair_messages(messages: list[dict]) -> list[dict]:
    """Pair consecutive user/assistant messages into exchanges with metadata."""
    pairs = []
    i = 0
    while i < len(messages) - 1:
        if messages[i]["role"] == "user" and messages[i + 1]["role"] == "assistant":
            exchange = {
                "prompt": messages[i]["text"],
                "response": messages[i + 1]["text"],
                "timestamp": messages[i]["timestamp"],
            }

            # Attach metadata (not vectorized, but stored alongside)
            metadata = {}
            if messages[i + 1].get("actions"):
                metadata["actions"] = messages[i + 1]["actions"]
            if messages[i + 1].get("thinking"):
                metadata["thinking_summary"] = messages[i + 1]["thinking"]
            if metadata:
                exchange["metadata"] = metadata

            pairs.append(exchange)
            i += 2
        else:
            i += 1
    return pairs


# ─────────────────────────────────────────────
#  Mnemo Cortex Client
# ─────────────────────────────────────────────

def ingest_exchange(prompt: str, response: str, metadata: dict = None) -> bool:
    """Send a single exchange to Mnemo Cortex /ingest."""
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["X-API-KEY"] = AUTH_TOKEN

    payload = {
        "prompt": prompt,
        "response": response,
        "agent_id": AGENT_ID,
    }
    if metadata:
        payload["metadata"] = metadata

    try:
        resp = httpx.post(
            f"{MNEMO_URL}/ingest",
            json=payload,
            headers=headers,
            timeout=5.0,
        )
        if resp.status_code == 200:
            return True
        else:
            log.warning(f"Ingest returned {resp.status_code}: {resp.text[:100]}")
            return False
    except Exception as e:
        log.warning(f"Ingest failed: {e}")
        return False


def check_mnemo_health() -> bool:
    """Check if Mnemo Cortex is reachable."""
    try:
        resp = httpx.get(f"{MNEMO_URL}/health", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


# ─────────────────────────────────────────────
#  Watcher Loop
# ─────────────────────────────────────────────

def process_session_file(filepath: Path, position: int) -> tuple[int, int]:
    """
    Read new lines from a session file starting at the given byte position.
    Returns (new_position, exchanges_ingested).
    """
    file_size = filepath.stat().st_size
    if file_size <= position:
        return position, 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(position)
        new_lines = f.readlines()
        new_position = f.tell()

    if not new_lines:
        return new_position, 0

    messages = parse_session_lines(new_lines)
    pairs = pair_messages(messages)

    ingested = 0
    for pair in pairs:
        # Merge source metadata with exchange metadata (actions, thinking)
        meta = {"source": "openclaw-watcher", "session_file": filepath.name}
        if pair.get("metadata"):
            meta.update(pair["metadata"])

        success = ingest_exchange(
            prompt=pair["prompt"],
            response=pair["response"],
            metadata=meta,
        )
        if success:
            ingested += 1

    return new_position, ingested


def run_watcher():
    """Main watcher loop."""
    log.info(f"⚡ Mnemo Cortex Session Watcher starting")
    log.info(f"  Watching:  {SESSIONS_DIR}")
    log.info(f"  Mnemo URL: {MNEMO_URL}")
    log.info(f"  Agent ID:  {AGENT_ID}")
    log.info(f"  State:     {STATE_FILE}")

    # Check Mnemo health
    if check_mnemo_health():
        log.info(f"  Mnemo:     ✓ connected")
    else:
        log.warning(f"  Mnemo:     ✗ not reachable (will retry)")

    positions = load_positions()
    consecutive_errors = 0
    save_counter = 0

    while True:
        try:
            # Find all active session files (not deleted, not reset archives)
            session_files = list(SESSIONS_DIR.glob("*.jsonl"))
            # Exclude .reset. and .deleted. files
            session_files = [
                f for f in session_files
                if ".reset." not in f.name and ".deleted." not in f.name
            ]

            total_ingested = 0

            for filepath in session_files:
                file_key = filepath.name
                current_pos = positions.get(file_key, 0)
                new_pos, ingested = process_session_file(filepath, current_pos)

                if new_pos != current_pos:
                    positions[file_key] = new_pos
                    total_ingested += ingested

            if total_ingested > 0:
                log.info(f"Ingested {total_ingested} new exchanges")
                save_positions(positions)
                consecutive_errors = 0

            # Periodic save (every ~30 seconds even without new data)
            save_counter += 1
            if save_counter >= 15:
                save_positions(positions)
                save_counter = 0

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Watcher error ({consecutive_errors}): {e}")
            if consecutive_errors >= 10:
                log.error("Too many consecutive errors, saving state and pausing 30s")
                save_positions(positions)
                time.sleep(30)
                consecutive_errors = 0

        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────
#  Backfill — ingest existing sessions
# ─────────────────────────────────────────────

def backfill_sessions(max_files: int = 10):
    """
    Ingest existing session files that haven't been processed yet.
    Run this once on first install to load history into Mnemo.
    """
    log.info(f"Backfilling up to {max_files} session files...")

    positions = load_positions()
    session_files = sorted(
        [f for f in SESSIONS_DIR.glob("*.jsonl")
         if ".reset." not in f.name and ".deleted." not in f.name],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:max_files]

    total = 0
    for filepath in session_files:
        file_key = filepath.name
        if file_key in positions:
            log.info(f"  Skipping {file_key} (already processed)")
            continue

        new_pos, ingested = process_session_file(filepath, 0)
        positions[file_key] = new_pos
        total += ingested
        log.info(f"  Backfilled {file_key}: {ingested} exchanges")

    save_positions(positions)
    log.info(f"Backfill complete: {total} total exchanges ingested")


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        max_files = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        backfill_sessions(max_files)
    else:
        try:
            run_watcher()
        except KeyboardInterrupt:
            log.info("Watcher stopped.")
            save_positions(load_positions())


# ─────────────────────────────────────────────
#  Systemd Service (install instructions)
# ─────────────────────────────────────────────
#
#  Save this as: ~/.config/systemd/user/mnemo-watcher.service
#
#  [Unit]
#  Description=Mnemo Cortex Session Watcher
#  After=network.target
#
#  [Service]
#  Type=simple
#  ExecStart=/usr/bin/python3 /home/guy/mnemo-cortex/agentb/watcher.py
#  Restart=always
#  RestartSec=5
#  Environment=MNEMO_URL=http://localhost:50001
#  Environment=MNEMO_AGENT_ID=rocky
#  Environment=PYTHONUNBUFFERED=1
#
#  [Install]
#  WantedBy=default.target
#
#  Then:
#    systemctl --user daemon-reload
#    systemctl --user enable --now mnemo-watcher
#
