# Mnemo Cortex — Claude Desktop Integration

MCP server that gives Claude Desktop persistent, cross-agent memory via Mnemo Cortex.

Claude Desktop gains three tools: recall past conversations, search across all agents, and save new memories. Memories survive across sessions and are shared with any other agent connected to the same Mnemo Cortex instance.

## Prerequisites

- **Node.js 18+**
- **A running Mnemo Cortex v2 server** — see the [main install guide](../../README.md)

## Install

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/claude-desktop
npm install
```

## Configure Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json` (Linux) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and add the `mcpServers` block:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/absolute/path/to/mnemo-cortex/integrations/claude-desktop/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "claude-desktop"
      }
    }
  }
}
```

Replace `/absolute/path/to` with the actual path where you cloned the repo. Set `MNEMO_AGENT_ID` to whatever you want this agent to be called in the memory system.

Restart Claude Desktop after saving.

## Available Tools

| Tool | Description |
|------|-------------|
| `mnemo_recall` | Recall memories for this agent. Semantic search over your past sessions. |
| `mnemo_search` | Search across ALL agents in the system. Optional `agent_id` filter. |
| `mnemo_save` | Save a summary and key facts to memory for future recall. |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MNEMO_URL` | `http://localhost:50001` | URL of your Mnemo Cortex API server |
| `MNEMO_AGENT_ID` | `claude-desktop` | Agent identity for storing and retrieving memories |

## How It Works

Claude Desktop spawns this server as a child process using stdio transport. When Claude uses one of the tools, the server makes HTTP requests to your Mnemo Cortex API:

- `mnemo_recall` → `POST /context` with the agent's own ID
- `mnemo_search` → `POST /context` without an agent filter (or with a specific one)
- `mnemo_save` → `POST /writeback` to store a session summary

## Multi-Agent Setup

If you run multiple agents (e.g., Claude Desktop + Claude Code + OpenClaw), give each a unique `MNEMO_AGENT_ID`. They all share the same Mnemo Cortex database, so any agent can search another's memories using `mnemo_search`.

## License

MIT
