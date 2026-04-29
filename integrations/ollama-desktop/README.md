# Mnemo Cortex — Ollama Desktop Integration

> ⚠️ **Read this first.** Ollama Desktop's native chat window does **not** support MCP / tools.
> You can't add Mnemo to Ollama Desktop's chat box directly. Anyone telling you otherwise (including an earlier version of this guide) is wrong.
>
> What you *can* do is use Ollama as the **LLM backend** for an MCP-aware app running on top. This guide shows the simplest path: `ollama launch openclaw` from a terminal.

Ollama Desktop is a great local model runner — fast, GPU-accelerated, easy model management. What it isn't: an MCP host. Its chat window is a plain LLM chat. To get persistent memory, you need an MCP-aware app sitting on top of it. Ollama Desktop ships with built-in launchers for several:

```
ollama launch claude        # Claude Code, with Ollama as the model
ollama launch openclaw      # OpenClaw, with Ollama as the model      ← we use this
ollama launch cline
ollama launch codex
ollama launch vscode
...
```

This guide walks the **OpenClaw** path: Ollama provides the LLM (e.g., qwen3:8b running locally), OpenClaw provides the agent runtime and MCP client, Mnemo plugs in via OpenClaw's MCP config.

If you'd rather have a desktop GUI chat with native MCP tool support — where the chat box itself does the saving and recalling — use **[LM Studio](../lmstudio/)** or **[AnythingLLM](../anythingllm/)** instead. Both can run Ollama models too. Pick the host that matches how you actually want to chat.

## Prerequisites

- **Ollama Desktop v0.20.0+** with built-in `ollama launch` — verified on 0.20.7
- **Node.js 18+** and **Git** on your PATH (OpenClaw is npm-installed)
- **A terminal** (Windows Command Prompt, PowerShell, macOS Terminal, Linux shell) — `ollama launch openclaw` is a CLI command, not something you type into Ollama Desktop's chat
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md). Local (`http://localhost:50001`) or remote both work.
- **A tool-capable model pulled** — `ollama pull qwen3:8b` is the proven tool-caller. Llama 3.2 Instruct, Mistral 7B v0.3, and Hermes-3 also work. See [Gotchas](#gotchas).

## Install (verified path on Windows 11, 2026-04-29)

### 1. Install OpenClaw

In a terminal:

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

> **Schema note:** current OpenClaw uses `mcp.servers` (nested under `mcp`). Older docs may show `mcpServers` at the root — that's the old schema and will fail validation on 2026.4.x.

### 4. First-time OpenClaw setup

```bash
openclaw config set gateway.mode local
openclaw doctor --fix
openclaw mcp list           # confirm: mnemo-cortex listed
```

### 5. Launch from a terminal — NOT from Ollama Desktop's chat

```bash
ollama launch openclaw
```

This is the part that trips people up. **You type this command in a terminal**, not in Ollama Desktop's chat input. Ollama Desktop's chat is a separate app — typing CLI commands into it just sends them as messages to the model.

When you run `ollama launch openclaw` in a terminal:

```
[ Windows terminal / PowerShell / macOS Terminal ]
   $ ollama launch openclaw
        │
        ▼
[ OpenClaw chat opens — TUI in same terminal, or web UI ]
   ← THIS chat is where Mnemo tools work.
   ← Save+recall happen here.

[ Ollama Desktop's GUI window — stays a plain chat. Never gets MCP. ]
```

Two separate UIs. Don't conflate them.

In OpenClaw's chat:
- *"Save a note: I prefer concise replies."* → model calls `mnemo_save`
- New session: *"What do you remember about my preferences?"* → model calls `mnemo_recall`

If both round-trip, the chain is wired.

## Verify (without driving the chat)

You can verify the bridge directly from a terminal, mimicking what OpenClaw will spawn:

```bash
MNEMO_URL=http://localhost:50001 \
MNEMO_AGENT_ID=ollama-yourname \
node /path/to/mnemo-cortex/integrations/openclaw-mcp/server.js
```

You should see `[mnemo-mcp] Connected to Mnemo Cortex (N memories, share: separate)` on stderr. Ctrl-C to exit. If you get a connection error, your `MNEMO_URL` is wrong or the server isn't reachable from this machine.

## Gotchas

### 1. Ollama Desktop's chat window will not get tools

Already said it twice but worth a third time. Ollama Desktop's native chat is a plain LLM chat with `tool_count=0`. Don't expect typing `mnemo_save` or `ollama launch openclaw` *in the chat* to work — those are CLI / system concepts, not chat content. Use a terminal.

### 2. Tool-capable model required

Even after you're in OpenClaw's chat, you need a model that actually emits MCP tool-use tokens. **Non-tool-capable models will narrate fake save IDs in their text response without actually invoking the tool.** Confirmed working: Qwen3 (any size), Mistral 7B Instruct v0.3, Hermes-3, Llama 3.2 Instruct. Confirmed *faking*: Llama 3.1 8B and similar older variants.

### 3. Network reachability for remote Mnemo servers

If your Mnemo server is on another machine (e.g., a home server reached via Tailscale), confirm reachability from a plain shell first:

```bash
curl http://YOUR_MNEMO_HOST:50001/health
```

If that fails, fix the network before fighting OpenClaw config. On Windows: install Tailscale, sign in, verify hostname resolves with `nslookup` (or just try `curl http://your-tailnet-host:50001/health`).

### 4. OpenClaw config schema drift

OpenClaw's MCP config schema has migrated:
- **Current (2026.4.x):** `mcp.servers.<name>` — nested under `mcp` key
- **Older:** `mcpServers.<name>` at config root

Use the current shape. `openclaw doctor` will tell you if your config doesn't validate.

### 5. `ollama launch openclaw` requires OpenClaw on PATH

Confirm `openclaw --version` works in the same shell first. On Windows, `npm install -g openclaw` puts it in `%APPDATA%\npm` — make sure that's on `PATH`.

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
| `BRAIN_DIR` | `~/mnemo-plan/brain` | Optional — enables brain-file tools when pointed at an existing dir |
| `WIKI_DIR` | unset | Optional — enables wiki tools when pointed at an existing dir |

## How It Works

```
[ Ollama Desktop GUI window ]   ← unrelated. Ignore for memory purposes.

[ Terminal ]
   $ ollama launch openclaw
       │
       ▼
[ OpenClaw agent runtime ]   ← gets Ollama as the LLM provider automatically
       │
       │ spawns child process via stdio
       ▼
[ openclaw-mcp/server.js (bridge) ]
       │
       │ HTTP POST to /writeback, /context
       ▼
[ Mnemo Cortex API @ MNEMO_URL ]
```

Ollama is the LLM. OpenClaw is the agent (handles tool calls, conversation state). The bridge is a small Node service that translates MCP stdio ↔ Mnemo REST. All embeddings happen server-side — no embedding-model mismatch is possible from this client.

## Workflow

For day-to-day use patterns, see the [Session Guide](../../SESSION-GUIDE.md). Same workflow applies whether your LLM is Claude, GPT, Gemini, or local Ollama.

## Next Step

**Read [THE-LANE-PROTOCOL.md](../../THE-LANE-PROTOCOL.md) to learn the session ritual that makes Mnemo actually work.**

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
