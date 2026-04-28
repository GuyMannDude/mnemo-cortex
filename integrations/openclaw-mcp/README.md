# Mnemo Cortex — OpenClaw MCP Integration

Give your OpenClaw bot a brain. One config line. Persistent memory.

Your bot remembers past conversations, recalls decisions, and shares knowledge with other agents in the system. Memories survive across sessions, across restarts, across everything.

## Prerequisites

- **Node.js 18+** (required for native fetch and ESM modules)
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
MNEMO_URL=http://your-mnemo-server:50001 node test.js
```

Tests cover both happy path (health, write, recall, cross-agent search) and failure cases (unreachable server, empty query, invalid endpoint).

## Available Tools

| Tool | What it does |
|------|-------------|
| `mnemo_recall` | Recall this agent's own memories. Semantic search over past sessions. |
| `mnemo_search` | Search memories. Restricted to own agent by default — use `mnemo_share` to enable cross-agent search. |
| `mnemo_save` | Save a summary and key facts. Use at session end or when something important happens. |
| `mnemo_share` | Toggle cross-agent memory sharing on/off for this session. |

## Sharing & Privacy

By default, each agent sees only its own memories. Cross-agent search is off.

### Share Modes

| Mode | `MNEMO_SHARE=` | Behavior |
|------|----------------|----------|
| **Separate** (default) | `separate` or unset | `mnemo_search` restricted to own agent. `mnemo_share` toggles per-session. |
| **Always** | `always` | Cross-agent search always on. For trusted teams where all agents are family. |
| **Never** | `never` | Cross-agent search permanently off. `mnemo_share` toggle blocked. |

### Priority

```
never  >  always  >  separate  >  session toggle
```

- `never` wins over everything — even if another part of your config says `always`, an agent set to `never` stays locked
- `always` means the toggle is a no-op (already on)
- `separate` (default) lets the agent toggle per-session with `mnemo_share`

### Example: Trusted Team

```bash
openclaw mcp set mnemo-cortex '{"command":"node","args":["/path/to/server.js"],"env":{"MNEMO_URL":"http://artforge:50001","MNEMO_AGENT_ID":"rocky","MNEMO_SHARE":"always"}}'
```

### Example: Isolated Client Project

```bash
openclaw mcp set mnemo-cortex '{"command":"node","args":["/path/to/server.js"],"env":{"MNEMO_URL":"http://artforge:50001","MNEMO_AGENT_ID":"client-bot","MNEMO_SHARE":"never"}}'
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address |
| `MNEMO_AGENT_ID` | `openclaw` | This agent's identity in the memory system |
| `MNEMO_SHARE` | `separate` | Cross-agent sharing mode: `separate`, `always`, or `never` |

## How It Works

OpenClaw spawns this server as a child process using MCP stdio transport. When your bot uses one of the memory tools, the server calls the Mnemo Cortex API:

- `mnemo_recall` → `POST /context` (filtered to this agent)
- `mnemo_search` → `POST /context` (gated by share mode)
- `mnemo_save` → `POST /writeback` (writes to this agent's memory slot)
- `mnemo_share` → toggles session share state (no API call)

All requests have a 10-second timeout. If Mnemo Cortex is unreachable, tools return clear error messages.

## Auto-Capture (in-bridge)

The bridge keeps a small ring buffer of tool activity and flushes a summary to Mnemo on its own. You don't need to call `mnemo_save` explicitly to get a record of what happened — manual saves are still recommended for high-signal moments (decisions, deliverables), but the bridge will log the rest of the trail for you.

| Setting | Value |
|---|---|
| Buffer size flush | 8 captureable tool calls |
| Idle flush | 2 minutes since last captureable call |
| Shutdown flush | `SIGTERM` / `SIGINT` drain on exit |
| Capture policy | `mnemo_recall`, `mnemo_search`, `read_brain_file`, `wiki_search`, `wiki_read` are captured as summaries; `mnemo_save` and `write_brain_file` are captured in full; `opie_startup`, `mnemo_share`, `*_index`, and most read-only `passport_*` tools skip capture (their content lives elsewhere already). |

### Where to find auto-capture entries in your memory store

Auto-capture flushes piggyback on the **active session ID**. If you've called `opie_startup` (or any tool that initialises a session), the flush lands under that same session — `session:opie-2026-04-28-07-21-01` — alongside any manual saves from the same session. This is intentional: it keeps related activity grouped on one timeline instead of fragmenting across `-auto-` sessions.

If no active session has been started, the flush falls back to `${AGENT_ID}-auto-{timestamp}` (e.g. `session:opie-auto-1777158029026`). You'll typically only see this pattern when an agent uses bridge tools without calling `opie_startup` first — most often a fresh test run or a non-Opie agent.

When debugging "did my auto-capture fire?", search by content (`[AUTO-CAPTURE] N tool calls:`) rather than by session-ID prefix. Filtering for `opie-auto-` will miss flushes that landed under an active session.

## Multi-Agent Memory

Every agent gets its own memory lane in the Mnemo Cortex database (keyed by `agent_id`). Agents write only to their own slot. Cross-agent reading is controlled by the share mode.

Give each bot a unique `MNEMO_AGENT_ID`. They all share the same Mnemo Cortex database — one memory spine, many agents.

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
