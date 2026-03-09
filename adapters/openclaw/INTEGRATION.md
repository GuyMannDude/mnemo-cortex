# OpenClaw Integration — Mnemo Cortex

## Overview

Mnemo Cortex integrates with OpenClaw through two mechanisms:

1. **Session Watcher** — captures every conversation TO Mnemo (automatic)
2. **Context Refresh** — writes Mnemo's memory context back to your workspace (automatic)

No OpenClaw hooks required. No JavaScript. No gateway risk.

## How It Works

```
Rocky chats with Guy
        │
   Watcher daemon (runs on IGOR)
   reads OpenClaw .jsonl session files
        │
   POST /ingest → Mnemo stores it
        │
   Refresh daemon (runs on IGOR)
   reads FROM Mnemo every 60 seconds
        │
   Writes MNEMO-CONTEXT.md to workspace
        │
   Rocky boots → reads MNEMO-CONTEXT.md
   alongside SOUL.md, MEMORY.md, etc.
```

## Setup (5 commands)

```bash
# Install
pip install mnemo-cortex

# Configure
mnemo-cortex init

# Start the memory server
mnemo-cortex start

# Start capturing sessions TO Mnemo
mnemo-cortex watch --backfill

# Start writing context FROM Mnemo to workspace
mnemo-cortex refresh --watch
```

## What Each Command Does

### `mnemo-cortex watch`
Monitors `~/.openclaw/agents/main/sessions/*.jsonl` in real-time.
Every user/assistant exchange (including tool calls and thinking blocks)
gets captured to Mnemo's vector database. If the session crashes,
everything up to the last exchange is already saved.

### `mnemo-cortex refresh --watch`
Every 60 seconds, queries Mnemo for recent context and writes it to
`~/.openclaw/workspace/MNEMO-CONTEXT.md`. OpenClaw's bootstrap reads
all `.md` files in the workspace root, so Rocky sees this at boot
alongside SOUL.md and MEMORY.md.

### Why Not Hooks?

We tried. OpenClaw workspace hooks have limitations:
- Different behavior across versions
- Silent failures that are hard to debug
- Can crash the gateway under certain conditions
- Property name changes between releases

The file-based approach works on every OpenClaw version, past and future.
It uses the same mechanism that makes SOUL.md and MEMORY.md work —
just a file sitting on disk that the agent reads.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| MNEMO_URL | http://localhost:50001 | Mnemo Cortex server |
| MNEMO_AGENT_ID | rocky | Agent ID for tenant isolation |

## Custom Workspace Path

If your OpenClaw workspace isn't at the default location:

```bash
mnemo-cortex refresh --watch -w /path/to/your/workspace
```

## Checking Status

```bash
mnemo-cortex status
```

Shows server health, session counts, watcher status, AND refresh daemon status.
