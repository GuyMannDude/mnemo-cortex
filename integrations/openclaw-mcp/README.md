# Mnemo Cortex — OpenClaw MCP Integration

Give your OpenClaw bot a brain. One config line. Persistent memory.

Your bot remembers past conversations, recalls decisions, and shares knowledge with other agents in the system. Memories survive across sessions, across restarts, across everything.

## Prerequisites

- **Node.js 18+**
- **A running Mnemo Cortex v2 server** — see the [main install guide](../../README.md)
- **OpenClaw 2026.3.7+** with MCP support

## Install

```bash
cd mnemo-cortex/integrations/openclaw-mcp
npm install
```

## Configure OpenClaw

One command:

```bash
openclaw mcp set mnemo-cortex '{"command":"node","args":["/absolute/path/to/mnemo-cortex/integrations/openclaw-mcp/server.js"],"env":{"MNEMO_URL":"http://artforge:50001","MNEMO_AGENT_ID":"rocky"}}'
```

Replace:
- `/absolute/path/to` with where you cloned the repo
- `MNEMO_URL` with your Mnemo Cortex server address
- `MNEMO_AGENT_ID` with your bot's name (e.g., `rocky`, `alice`, `sparky`)

Restart OpenClaw after:

```bash
systemctl --user restart openclaw-gateway
```

## Verify

```bash
openclaw mcp list
```

You should see `mnemo-cortex` in the list.

Then talk to your bot and ask: "What do you remember?" — it should use the `mnemo_recall` tool.

## Test the Connection

Before configuring OpenClaw, verify the Mnemo Cortex API is reachable:

```bash
MNEMO_URL=http://artforge:50001 node test.js
```

All four tests should pass.

## Available Tools

| Tool | What it does |
|------|-------------|
| `mnemo_recall` | Recall this agent's own memories. Semantic search over past sessions. |
| `mnemo_search` | Search across ALL agents. Find what Rocky tested, what CC built, what Opie designed. |
| `mnemo_save` | Save a summary and key facts. Use at session end or when something important happens. |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address |
| `MNEMO_AGENT_ID` | `openclaw` | This agent's identity in the memory system |

## How It Works

OpenClaw spawns this server as a child process using MCP stdio transport. When your bot uses one of the memory tools, the server calls the Mnemo Cortex API:

- `mnemo_recall` → `POST /context` (filtered to this agent)
- `mnemo_search` → `POST /context` (all agents or filtered)
- `mnemo_save` → `POST /writeback` (writes to this agent's memory slot)

## Multi-Agent Memory

Every agent gets its own memory lane. Rocky can't overwrite Opie's memories, but any agent can read any other agent's memories using `mnemo_search`.

Give each bot a unique `MNEMO_AGENT_ID`. They all share the same Mnemo Cortex database — one memory spine, many agents.

```
Rocky writes to memory/rocky/
CC writes to memory/cc/
Opie writes to memory/opie/
Your bot writes to memory/your-bot/
```

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
