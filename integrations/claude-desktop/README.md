# Mnemo Cortex MCP Bridge

MCP server that connects Claude Desktop (or any MCP client) to Mnemo Cortex — giving your AI persistent semantic memory across sessions.

## What it does

Without this bridge, every Claude Desktop conversation starts from zero. With it, Claude can:

- **Recall** past conversations by meaning, not just keywords
- **Search** across multiple agents
- **Save** summaries and key facts for future sessions
- **Read/write brain files** — markdown files that serve as persistent identity and state

## Tools (7)

| Tool | Description |
|------|-------------|
| `mnemo_recall` | Semantic recall for the current agent |
| `mnemo_search` | Cross-agent search across all agents |
| `mnemo_save` | Save summaries or key facts for future recall |
| `opie_startup` | Full orientation loader — brain lane + recent memory |
| `read_brain_file` | Read any brain lane file |
| `list_brain_files` | List all brain lane files |
| `write_brain_file` | Update brain lane files |

## Setup

```bash
cd integrations/claude-desktop
npm install
```

Add to `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/path/to/mnemo-cortex/integrations/claude-desktop/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "your-agent-name",
        "BRAIN_DIR": "/path/to/your/brain/directory"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear automatically.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MNEMO_URL` | `http://localhost:50001` | URL of your Mnemo Cortex instance |
| `MNEMO_AGENT_ID` | `default` | Agent identity for recall and save |
| `BRAIN_DIR` | `~/.mnemo-cortex/brain` | Directory for brain lane markdown files |

## How it works

```
Claude Desktop  <-->  MCP bridge (stdio)  <-->  Mnemo Cortex API (HTTP)
                                          <-->  Brain files (filesystem)
```

Memory is semantic — queries are matched by meaning using embeddings, not exact keywords.

## License

MIT — Project Sparks / Guy Hutchins
