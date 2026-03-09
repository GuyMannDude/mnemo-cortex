#!/usr/bin/env python3
"""
Mnemo Cortex Refresher Daemon
==============================
Background process that periodically writes MNEMO-CONTEXT.md to the agent workspace.
Launched by `mnemo-cortex refresh --watch`.

Usage:
    python3 refresher.py <workspace_path> <output_filename> <recent_count> <interval_seconds>

Environment:
    MNEMO_URL       - Mnemo Cortex server (default: http://localhost:50001)
    MNEMO_AGENT_ID  - Agent ID (default: rocky)
"""

import os
import sys
import time
import logging
from pathlib import Path

import httpx

MNEMO_URL = os.environ.get("MNEMO_URL", "http://localhost:50001")
AGENT_ID = os.environ.get("MNEMO_AGENT_ID", "rocky")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mnemo-refresh")


def fetch_context(recent: int) -> str:
    """Fetch context from Mnemo Cortex."""
    context_text = ""

    # Try /sessions/recent first
    try:
        resp = httpx.get(
            f"{MNEMO_URL}/sessions/recent",
            params={"agent_id": AGENT_ID, "n": recent},
            timeout=5,
        )
        if resp.status_code == 200:
            context_text = resp.json().get("context", "")
    except Exception as e:
        log.debug(f"Recent endpoint failed: {e}")

    # Fallback to /context search
    if not context_text.strip():
        try:
            resp = httpx.post(
                f"{MNEMO_URL}/context",
                json={
                    "prompt": "recent project status active tasks",
                    "agent_id": AGENT_ID,
                    "max_results": 3,
                },
                timeout=8,
            )
            if resp.status_code == 200:
                chunks = resp.json().get("chunks", [])
                if chunks:
                    context_text = "\n\n---\n\n".join(
                        f"[{c.get('cache_tier', '?')}|{c.get('relevance', '?')}] {c.get('content', '')}"
                        for c in chunks
                    )
        except Exception as e:
            log.debug(f"Context endpoint failed: {e}")

    return context_text


def write_context(workspace: Path, filename: str, context: str):
    """Write context to the workspace file."""
    output = workspace / filename
    header = (
        "# ⚡ Mnemo Cortex — Memory Context\n"
        f"_Auto-refreshed at {time.strftime('%Y-%m-%d %H:%M:%S')}_\n"
        f"_Agent: {AGENT_ID} | Source: {MNEMO_URL}_\n\n"
    )
    output.write_text(header + context + "\n")


def main():
    if len(sys.argv) < 5:
        print(f"Usage: {sys.argv[0]} <workspace> <filename> <recent> <interval>")
        sys.exit(1)

    workspace = Path(sys.argv[1])
    filename = sys.argv[2]
    recent = int(sys.argv[3])
    interval = int(sys.argv[4])

    log.info(f"⚡ Mnemo Cortex Refresher starting")
    log.info(f"  Workspace: {workspace}")
    log.info(f"  Output:    {workspace / filename}")
    log.info(f"  Mnemo:     {MNEMO_URL}")
    log.info(f"  Agent:     {AGENT_ID}")
    log.info(f"  Interval:  {interval}s")

    # Check Mnemo health
    try:
        resp = httpx.get(f"{MNEMO_URL}/health", timeout=3)
        if resp.status_code == 200:
            log.info(f"  Mnemo:     ✓ connected")
        else:
            log.warning(f"  Mnemo:     ✗ unhealthy (will retry)")
    except Exception:
        log.warning(f"  Mnemo:     ✗ not reachable (will retry)")

    consecutive_failures = 0

    while True:
        try:
            context = fetch_context(recent)
            if context.strip():
                write_context(workspace, filename, context)
                log.info(f"Refreshed ({len(context)} chars)")
                consecutive_failures = 0
            else:
                log.debug("No context available")
        except Exception as e:
            consecutive_failures += 1
            log.warning(f"Refresh failed: {e}")
            if consecutive_failures > 10:
                log.error("Too many consecutive failures, backing off")
                time.sleep(interval * 5)
                consecutive_failures = 0

        time.sleep(interval)


if __name__ == "__main__":
    main()
