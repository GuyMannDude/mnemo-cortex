# Mnemo Cortex — AnythingLLM Integration

Persistent semantic memory inside AnythingLLM workspaces. Drop in the MCP config, flip the workspace to **Automatic mode**, and memory tools fire on every message — no `@agent` prefix, just normal conversation.

AnythingLLM speaks MCP through its plugin layer. With this integration, every workspace that opts in gains `mnemo_save`, `mnemo_recall`, `mnemo_search`, `mnemo_share`, plus the optional brain-file and wiki tools. Memories survive restarts, workspace switches, and full app reinstalls.

## Prerequisites

- **AnythingLLM Desktop** (recent build with MCP plugin layer enabled)
- **Node.js 18+** on your PATH (for the bridge)
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md). Local (`http://localhost:50001`) or remote both work.
- **A tool-capable model.** Verified working: **Qwen3 (any size)**, **Mistral 7B Instruct v0.3**, **GPT-4 / GPT-4o** (OpenAI provider), **Claude 3.5+** (Anthropic provider). See [Gotchas](#gotchas) — `llama3.1:8b` and similar will hallucinate tool calls instead of invoking them.

## Install

### 1. Clone the bridge

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/openclaw-mcp
npm install
```

The bridge is the same Node service used by every Mnemo MCP integration.

### 2. Edit `anythingllm_mcp_servers.json`

Open AnythingLLM's MCP plugin config:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\anythingllm-desktop\storage\plugins\anythingllm_mcp_servers.json` |
| macOS | `~/Library/Application Support/anythingllm-desktop/storage/plugins/anythingllm_mcp_servers.json` |
| Linux | `~/.config/anythingllm-desktop/storage/plugins/anythingllm_mcp_servers.json` |

If the file or its parent directory don't exist, create them. Add this entry under `mcpServers`:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "anythingllm",
        "MNEMO_SHARE": "separate"
      }
    }
  }
}
```

Replace `/ABSOLUTE/PATH/TO` with where you cloned the repo. Adjust `MNEMO_URL` if your Mnemo server is remote.

### 3. Flip the workspace to Automatic mode (important)

By default, AnythingLLM workspaces require an `@agent` prefix on every message that needs tools. **Automatic mode removes that requirement** — tools fire whenever the model decides they're useful.

1. Open the workspace where you want memory active.
2. Click the **⚙️ Settings** icon (next to the workspace name).
3. Go to the **Chat Settings** tab.
4. Change **Chat mode** to **Automatic**.

Per [AnythingLLM's docs](https://docs.anythingllm.com/features/chat-modes), Automatic mode "automatically uses all available agent-skills, tools, and MCPs." That's exactly what we want for memory — `mnemo_save` and `mnemo_recall` fire whenever the model decides they're useful, without you typing `@agent`.

> **Visual cue:** if the chat input shows an `@` symbol on the left, you're still in default mode and need `@agent` per message. If the `@` is gone, Automatic mode is active and memory just works.

### 4. Restart AnythingLLM

Quit fully (system tray on Windows, Cmd-Q on macOS) and reopen. The MCP config is read at startup.

## Verify

1. Open a chat in your Automatic-mode workspace.
2. Type:

> **You:** Save a note that I'm using AnythingLLM with Mnemo Cortex.

The model should respond *and* you'll see a tool-call indicator (model-and-platform-dependent — usually a small "tool used" badge or expandable invocation block).

3. **Verify the save actually happened.** Open a *new* chat in the same workspace and ask:

> **You:** What do you remember about my AnythingLLM setup?

The model should call `mnemo_recall` and surface what you saved. If it doesn't, see [Gotcha #1](#1-non-tool-capable-models-silently-fake-success).

## Fallback: default mode with `@agent` prefix

If your workspace can't run Automatic mode (model doesn't support native tool calling, agent skills disabled, etc.), you can stay in default mode and prefix tool-using messages with `@agent`:

> **You:** `@agent please save a memory using mnemo_save: I prefer concise replies.`

This works but adds friction to every memory-relevant message. Automatic mode with a tool-capable model is the better path.

## Gotchas

These are **verified failures from real installs** (Windows 11 testing, 2026-04-27). Not theoretical.

### 1. Non-tool-capable models silently fake success

This is the most common confusion. With a non-tool-capable model:

> **Llama 3.1 8B (NOT tool-capable):** "I've saved that to memory with id `e4d3c9f1`."

The memory ID is **hallucinated**. Nothing was saved. The model is performing what it thinks a tool call looks like in its training data, never actually emitting MCP tool-use tokens.

**Fix:** Use a tool-capable model.
- ✅ **Confirmed working:** Qwen3 (any size), Mistral 7B Instruct v0.3, GPT-4 / GPT-4o, Claude 3.5+
- ❌ **Confirmed faking:** Llama 3.1 8B, older Llama variants

Same bridge, same Mnemo server — only the model matters.

### 2. The GUI may show one model while `.env` runs another

AnythingLLM's GUI displays whatever you last selected, but `.env` (at `%APPDATA%\anythingllm-desktop\storage\.env` on Windows or equivalent on other platforms) retains a stale `OLLAMA_MODEL_PREF` from a previous selection until you fully restart. Result: you "switch" to qwen3 in the GUI, the `.env` still says llama3.1, and your tools fail in the way described in Gotcha #1.

**Fix:** After switching models, **fully restart AnythingLLM**. Verify by opening `.env` and confirming `OLLAMA_MODEL_PREF` matches what the GUI shows.

### 3. Auto-discovered Ollama URL may not have your model

`OLLAMA_BASE_PATH` in `.env` defaults to auto-discovery. On a multi-machine network, this can land on an Ollama instance that doesn't have the model you loaded locally — and AnythingLLM will appear to use a *different* model than the one you pulled.

**Fix:** Pin `OLLAMA_BASE_PATH` to the exact host:
- Local Ollama: `http://localhost:11434`
- Remote: `http://10.0.0.X:11434`

Restart AnythingLLM after changing `.env`.

### 4. Server unreachable in mid-chat

If Mnemo Cortex goes down while AnythingLLM is running, the next memory tool call returns an error. Tool-capable models handle this gracefully; weaker models may get confused. Best practice: `curl http://localhost:50001/health` before opening AnythingLLM if you suspect server issues.

## Sharing & Privacy

By default, each agent sees only its own memories.

| Mode | `MNEMO_SHARE=` | Behavior |
|---|---|---|
| **Separate** (default) | `separate` or unset | Search restricted to own agent. `mnemo_share` toggles per-session. |
| **Always** | `always` | Cross-agent search always on. For trusted teams. |
| **Never** | `never` | Cross-agent search permanently off. |

If you run multiple AnythingLLM workspaces and want them isolated, give each its own `MNEMO_AGENT_ID` (`anythingllm-personal`, `anythingllm-work`, etc.).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address |
| `MNEMO_AGENT_ID` | `openclaw` (rename to `anythingllm`) | Agent identity |
| `MNEMO_SHARE` | `separate` | Cross-agent sharing mode |
| `BRAIN_DIR` | unset | Optional — enables brain-file tools when pointed at an existing dir |
| `WIKI_DIR` | unset | Optional — enables wiki tools when pointed at an existing dir |

## Multi-Workspace Memory

AnythingLLM lets you run multiple workspaces in parallel. By default, all workspaces sharing the same `mnemo-cortex` MCP entry will share the same `MNEMO_AGENT_ID` and therefore the same memory lane.

To isolate workspaces:

1. Duplicate the MCP entry under a different name (e.g., `mnemo-cortex-personal`, `mnemo-cortex-work`).
2. Give each a distinct `MNEMO_AGENT_ID`.
3. Enable only the relevant entry per workspace via AnythingLLM's per-workspace MCP toggles.

Each lane is independent. Cross-search across lanes only happens if `MNEMO_SHARE=always` (or you explicitly call `mnemo_share` to enable it for a session).

## How It Works

AnythingLLM spawns the Mnemo Cortex bridge (`openclaw-mcp/server.js`) as a child process via MCP stdio transport. When your model invokes a memory tool inside an Automatic-mode workspace, the bridge calls Mnemo Cortex's REST API:

- `mnemo_recall` → `POST /context` (your agent only)
- `mnemo_search` → `POST /context` (cross-agent gated by share mode)
- `mnemo_save` → `POST /writeback`
- `mnemo_share` → toggles session share state (no API call)

10-second timeout per request. Bridge logs to stderr (visible in AnythingLLM's developer console if you enable verbose logging).

## Workflow

See the [Session Guide](../../SESSION-GUIDE.md) for day-to-day use patterns — when to recall, when to save, how to structure a brain file.

## Next Step

**Read [THE-LANE-PROTOCOL.md](../../THE-LANE-PROTOCOL.md) to learn the session ritual that makes Mnemo actually work.**

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
