# Mnemo Cortex — Ollama Desktop Integration

Persistent memory for Ollama Desktop via its built-in **OpenClaw** integration. Your local Ollama-driven chats remember across sessions, share memory with other agents, and run on whatever tool-capable open-weights model you pulled.

Ollama Desktop ships with first-class integrations for Claude Code, Cline, Codex, OpenClaw, VS Code, and others — all launched via `ollama launch <integration>`. We use the **OpenClaw** path: Ollama provides the LLM, OpenClaw provides the agent runtime + MCP host, Mnemo plugs in via OpenClaw's MCP config.

## Prerequisites

- **Ollama Desktop v0.20.0+** with built-in `ollama launch` — verified on 0.20.7
- **Node.js 18+** and **Git** on your PATH (OpenClaw is npm-installed)
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md). Local (`http://localhost:50001`) or remote both work.
- **A tool-capable model pulled** — `ollama pull qwen3:8b` is the proven tool-caller. Llama 3.2 Instruct, Mistral 7B v0.3, and Hermes-3 also work. See [Gotchas](#gotchas).

## Install (verified path on Windows 11 + IGOR-2 2026-04-29)

### 1. Install OpenClaw

```bash
npm install -g openclaw
openclaw --version          # confirm: OpenClaw 2026.4.26+ (or current)
```

### 2. Clone the Mnemo bridge code

```bash
mkdir -p ~/github && cd ~/github
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/openclaw-mcp && npm install
```

(Windows: replace `~/github` with `%USERPROFILE%\github` and adjust paths.)

### 3. Wire Mnemo MCP into OpenClaw

OpenClaw stores config at `~/.openclaw/openclaw.json` (Windows: `%USERPROFILE%\.openclaw\openclaw.json`). Create or edit it to include:

```json
{
  "mcp": {
    "servers": {
      "mnemo-cortex": {
        "command": "node",
        "args": [
          "/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"
        ],
        "env": {
          "MNEMO_URL": "http://localhost:50001",
          "MNEMO_AGENT_ID": "ollama-yourname",
          "MNEMO_SHARE": "separate"
        }
      }
    }
  }
}
```

Replace `/ABSOLUTE/PATH/TO` with your clone location. Adjust `MNEMO_URL` if Mnemo is on another host. Pick a unique `MNEMO_AGENT_ID` (e.g., `ollama-laptop`, `ollama-igor2`) so memories don't collide with other agents.

> Schema note: current OpenClaw uses `mcp.servers` (nested under `mcp`). Older docs may show `mcpServers` at the root — that's the old schema and will fail validation on 2026.4.x.

### 4. First-time OpenClaw setup

```bash
openclaw config set gateway.mode local
openclaw doctor --fix
openclaw mcp list           # confirm: mnemo-cortex listed
```

### 5. Launch Ollama → OpenClaw → Mnemo

```bash
ollama launch openclaw
```

Ollama spawns OpenClaw with your local Ollama instance as the LLM provider. OpenClaw spawns the Mnemo MCP bridge as a child process. The bridge connects to your Mnemo server.

In the chat:
- *"Save a note: I prefer concise replies."* → model calls `mnemo_save`
- New session: *"What do you remember about my preferences?"* → model calls `mnemo_recall`

If both round-trip, the chain is wired.

## Verify

Without driving the chat, you can verify the bridge from a terminal:

```bash
# From a clean shell, mimicking what OpenClaw spawns:
MNEMO_URL=http://localhost:50001 \
MNEMO_AGENT_ID=ollama-yourname \
node /path/to/mnemo-cortex/integrations/openclaw-mcp/server.js
```

You should see `[mnemo-mcp] Connected to Mnemo Cortex (N memories, share: separate)` on stderr. Ctrl-C to exit. If you get a connection error, your `MNEMO_URL` is wrong or the server isn't reachable.

## Gotchas

### 1. Tool-capable model required

Same warning as the LM Studio and AnythingLLM integrations: **non-tool-capable models will narrate fake save IDs in their text response without actually invoking the tool.** Stick with confirmed tool-callers — `qwen3` (any size), Mistral 7B v0.3, Hermes-3, Llama 3.2 Instruct.

`llama3.1:8b` is *not* tool-capable in this sense; it will pretend to save without saving.

### 2. Network reachability for remote Mnemo servers

If your Mnemo server is on another machine (e.g., a home server reached via Tailscale), make sure the host running Ollama Desktop can reach it from a plain shell first:

```bash
curl http://YOUR_MNEMO_HOST:50001/health
```

If that fails, fix the network before fighting OpenClaw config. On Windows: install Tailscale, sign in, verify hostname resolves with `nslookup` (or just try `curl http://your-tailnet-host:50001/health`).

### 3. OpenClaw config schema drift

OpenClaw's MCP config schema has migrated:

- **Current (2026.4.x):** `mcp.servers.<name>` — nested under `mcp` key
- **Older:** `mcpServers.<name>` at config root

Use the current shape. `openclaw doctor` will tell you if your config doesn't validate.

### 4. `ollama launch openclaw` doesn't start unless OpenClaw is on PATH

Confirm `openclaw --version` works in the same shell first. On Windows, `npm install -g openclaw` puts it in `%APPDATA%\npm` — make sure that's on PATH.

## Sharing & Privacy

| Mode | `MNEMO_SHARE=` | Behavior |
|---|---|---|
| **Separate** (default) | `separate` | Search restricted to own agent. `mnemo_share` toggles per-session. |
| **Always** | `always` | Cross-agent search always on. For trusted teams. |
| **Never** | `never` | Cross-agent search permanently off. |

Pick `always` if you want this Ollama-driven agent to read what your other agents (Claude Desktop, OpenClaw bots, Claude Code) have learned. Pick `separate` if you want it isolated.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address |
| `MNEMO_AGENT_ID` | `openclaw` (rename per host) | This agent's identity in the memory system |
| `MNEMO_SHARE` | `separate` | Cross-agent sharing mode |
| `BRAIN_DIR` | `~/mnemo-plan/brain` | Optional — enables brain-lane tools when pointed at an existing dir |
| `WIKI_DIR` | unset | Optional — enables wiki tools when pointed at an existing dir |

## How It Works

```
[ollama launch openclaw]
    │
    ▼
[Ollama Desktop] ──launches──▶ [OpenClaw agent runtime]
                                       │
                                       │ spawns child process via stdio
                                       ▼
                               [openclaw-mcp/server.js]
                                       │
                                       │ HTTP POST to /writeback, /context
                                       ▼
                               [Mnemo Cortex API @ MNEMO_URL]
```

Ollama is the LLM. OpenClaw is the agent (handles tool calls, conversation state). The bridge is a small Node service that translates MCP stdio ↔ Mnemo REST. All embeddings happen server-side — no embedding-model mismatch is possible from this client.

## Workflow

For day-to-day use patterns, see the [Session Guide](../../SESSION-GUIDE.md). Same workflow applies whether your LLM is Claude, GPT, Gemini, or local Ollama.

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
