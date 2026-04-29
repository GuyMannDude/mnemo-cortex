# Mnemo Cortex — LM Studio Integration

Give your local LM Studio model persistent memory across chats and sessions. One config file, restart, done.

LM Studio added native MCP client support in **v0.3.17**. With this integration, your LM Studio chats — running entirely on your machine, on any tool-capable open-weights model — gain persistent semantic memory, brain-file file access, and (optional) cross-agent search alongside other agents on your network.

## Prerequisites

- **LM Studio v0.3.17 or later** — earlier versions have no MCP client
- **Node.js 18+** on your PATH (for the bridge)
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md). It can be on this machine (`http://localhost:50001`) or anywhere reachable on your network.
- **A tool-capable model loaded in LM Studio.** Tested working: **Qwen3 (any size)**, **Llama 3.2 3B Instruct**, **Mistral 7B Instruct v0.3**, **Hermes-3 Llama 3.1 8B**. Models without tool-calling ability will appear to "succeed" while never actually calling Mnemo — see [Gotchas](#gotchas) below.

## Install

### 1. Clone the bridge

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/openclaw-mcp
npm install
```

The bridge lives in `openclaw-mcp/` — it's the same Node service used by every Mnemo MCP integration (LM Studio, Claude Desktop, OpenClaw, etc.).

### 2. Edit `mcp.json`

Open LM Studio's MCP config file:

| Platform | Path |
|---|---|
| Windows | `%USERPROFILE%\.lmstudio\mcp.json` |
| macOS | `~/.lmstudio/mcp.json` |
| Linux | `~/.lmstudio/mcp.json` |

If the file doesn't exist, create it. Add this entry under `mcpServers`:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "lmstudio",
        "MNEMO_SHARE": "separate"
      }
    }
  }
}
```

Replace `/ABSOLUTE/PATH/TO` with where you cloned the repo. Adjust `MNEMO_URL` if your Mnemo server is remote.

### 3. Restart LM Studio

Fully quit LM Studio (not just close the window) and reopen. The MCP config is read at startup only.

## Verify

1. Open a chat with a tool-capable model.
2. Click the **MCP** tab in the chat panel (right side of the chat input).
3. You should see `mnemo-cortex` listed with **9 tools**:
   - 4 memory tools: `mnemo_recall`, `mnemo_search`, `mnemo_save`, `mnemo_share`
   - 5 Passport tools: `passport_get_user_context`, `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override`

If `BRAIN_DIR` and/or `WIKI_DIR` are also set (see [Optional](#optional-brain-file-and-wiki-tools)), you'll see additional tools.

### Quick functional test

In a new chat:

> **You:** Save a note that I prefer concise replies.

The model should call `mnemo_save` (you'll see the tool invocation in the chat). Then start a *new chat*:

> **You:** What do you remember about my preferences?

The model should call `mnemo_recall` and surface "concise replies." That round-trip across separate chats confirms persistence.

## Optional: brain file and wiki tools

The bridge can expose project-context tools when you set extra env vars:

```json
"env": {
  "MNEMO_URL": "http://localhost:50001",
  "MNEMO_AGENT_ID": "lmstudio",
  "BRAIN_DIR": "/path/to/your/mnemo-plan/brain",
  "WIKI_DIR": "/path/to/your/wiki"
}
```

When `BRAIN_DIR` points at an existing directory, these tools auto-register:
- `read_brain_file`, `write_brain_file`, `list_brain_files`
- `opie_startup` (session-start context bundle)
- `session_end` (writeback + brain commit)

When `WIKI_DIR` points at an existing directory, these tools register:
- `wiki_search`, `wiki_read`, `wiki_index`

Both are optional. Memory tools work without either. See the [mnemo-plan template](https://github.com/GuyMannDude/mnemo-plan) for a starter brain repo.

## Gotchas

These are real, verified failure modes — not theoretical:

### 1. Non-tool-capable models silently fake success

This is the single biggest pitfall on LM Studio. Some open-weights models will *narrate* tool calls in their text response without actually invoking them:

> **Llama 3.1 8B (NOT tool-capable):** "I've saved that to memory with id `e4d3c9f1`."

The memory ID is **hallucinated**. Nothing was saved. The model is performing what it thinks a tool call looks like in its training data, not actually emitting structured tool-use tokens that LM Studio's MCP client can parse.

**Fix:** Use a model with native tool-calling support. Qwen3 (any size), Llama 3.2 Instruct, Mistral 7B v0.3, and Hermes-3 are confirmed working.

**Verify it's actually calling tools** by opening the **Tool Calls** panel in LM Studio — real invocations show up there with structured arguments. If the chat says "saved" but Tool Calls is empty, the model faked it.

### 2. Bridge changes need a full LM Studio restart

LM Studio reads `mcp.json` only at app launch. If you edit the config (change `MNEMO_AGENT_ID`, point at a different server, etc.), close LM Studio fully — including from the system tray on Windows — and reopen. Reloading the model is not enough.

### 3. Server unreachable produces a mid-chat tool error

If Mnemo Cortex goes down while LM Studio is running, the next memory tool invocation will return an error to the model. Most tool-capable models handle this gracefully ("I can't reach memory right now, but..."). Some weaker models will get confused. Best practice: run a quick `curl http://localhost:50001/health` before opening LM Studio if you suspect server issues.

## Sharing & Privacy

By default, each agent sees only its own memories. Cross-agent search is off.

| Mode | `MNEMO_SHARE=` | Behavior |
|---|---|---|
| **Separate** (default) | `separate` or unset | Search restricted to own agent. `mnemo_share` toggles per-session. |
| **Always** | `always` | Cross-agent search always on. For trusted teams. |
| **Never** | `never` | Cross-agent search permanently off. Toggle blocked. |

Use `separate` if you also run other agents (Claude Desktop, OpenClaw, Claude Code) and want LM Studio kept independent. Use `always` if you want LM Studio to read what those other agents have learned.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address |
| `MNEMO_AGENT_ID` | `openclaw` (rename to `lmstudio`) | This agent's identity in the memory system |
| `MNEMO_SHARE` | `separate` | Cross-agent sharing mode |
| `BRAIN_DIR` | unset | Optional — enables brain-file tools when set |
| `WIKI_DIR` | unset | Optional — enables wiki tools when set |

## How It Works

LM Studio spawns the Mnemo Cortex bridge (`openclaw-mcp/server.js`) as a child process using MCP stdio transport. When your LM Studio model invokes a memory tool, the bridge calls Mnemo Cortex's REST API:

- `mnemo_recall` → `POST /context` (your agent only)
- `mnemo_search` → `POST /context` (cross-agent gated by share mode)
- `mnemo_save` → `POST /writeback`
- `mnemo_share` → toggles session share state (no API call)

All requests have a 10-second timeout. The bridge itself logs to stderr (visible in LM Studio's developer console).

## Workflow

For day-to-day use patterns, see the [Session Guide](../../SESSION-GUIDE.md). It covers when to recall, when to save, how to structure a brain file, and per-platform boot snippets.

## Next Step

**Read [THE-LANE-PROTOCOL.md](../../THE-LANE-PROTOCOL.md) to learn the session ritual that makes Mnemo actually work.**

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
