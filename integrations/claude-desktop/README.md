# Mnemo Cortex — Claude Desktop Integration

Persistent memory for Claude Desktop on **Windows, Mac, and Linux**. Same recall, save, and search tools that OpenClaw agents use — just wired through Claude Desktop's MCP config.

## What You Need

- **Mnemo Cortex** running somewhere (this machine or a remote server on your network)
- **Claude Desktop** installed
- **Node.js 18+** (for the MCP server)

## Install

### 1. Clone the repo (if you haven't already)

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/openclaw-mcp
npm install
```

The MCP server lives in `integrations/openclaw-mcp/` — it's platform-agnostic and works with Claude Desktop, not just OpenClaw.

### 2. Find your Claude Desktop config file

| Platform | Config path |
|----------|-------------|
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` |
| **Mac** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Linux** | `~/.config/Claude/claude_desktop_config.json` |

You can also open it from Claude Desktop: **Settings > Developer > Edit Config**

### 3. Add Mnemo Cortex to the config

Edit the config file and add a `mnemo-cortex` entry under `mcpServers`:

**Mac / Linux:**

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/absolute/path/to/mnemo-cortex/integrations/openclaw-mcp/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "opie"
      }
    }
  }
}
```

**Windows:**

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["C:\\Users\\YourName\\mnemo-cortex\\integrations\\openclaw-mcp\\server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "opie"
      }
    }
  }
}
```

Replace:
- The path with where you actually cloned the repo
- `MNEMO_URL` with your Mnemo Cortex server address
- `MNEMO_AGENT_ID` with a name for this Claude Desktop instance (e.g., `opie`, `desktop`, `my-claude`)

### 4. Restart Claude Desktop

Close and reopen Claude Desktop. The MCP server starts automatically.

### 5. Verify

Ask Claude: **"What do you remember?"**

It should use the `mnemo_recall` tool. If it does, you're live.

## Available Tools

Once connected, Claude Desktop gets four memory tools:

| Tool | What it does |
|------|-------------|
| `mnemo_recall` | Recall this agent's own memories. Semantic search over past sessions. |
| `mnemo_search` | Search all memories. Cross-agent access controlled by share mode. |
| `mnemo_save` | Save a summary and key facts. Use when something important happens. |
| `mnemo_share` | Toggle cross-agent memory sharing on/off for this session. |

## Remote Server Setup

If Mnemo Cortex runs on a different machine (common in multi-machine setups), just point `MNEMO_URL` at that machine:

```json
"MNEMO_URL": "http://your-server:50001"
```

The server must be reachable from the machine running Claude Desktop. If both machines are on the same network (LAN, Tailscale, etc.), this works out of the box.

## Multi-Agent Memory

Every Claude Desktop instance can have its own `MNEMO_AGENT_ID`. They all share the same Mnemo Cortex database. Agent A can search Agent B's memories if sharing is enabled.

Give each instance a unique ID. One memory spine, many agents.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address |
| `MNEMO_AGENT_ID` | `openclaw` | This agent's identity in the memory system |
| `MNEMO_SHARE` | `separate` | Cross-agent sharing mode: `separate`, `always`, or `never` |

## Troubleshooting

**Tools don't appear in Claude Desktop** — Restart Claude Desktop completely (quit, not just close the window). Check that the path to `server.js` is correct and absolute.

**"Mnemo Cortex unreachable"** — The server isn't running or the URL is wrong. Test from a terminal:
```bash
curl http://localhost:50001/health
```

**Node not found** — Make sure Node.js 18+ is installed and on your PATH. On Windows, you may need the full path to `node.exe`.

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
