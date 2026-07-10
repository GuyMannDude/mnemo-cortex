# Mnemo Cortex — OpenAI Codex CLI Integration

Give Codex CLI persistent memory and a shared brain: one MCP config block, one instructions file, done.

Codex CLI has native MCP client support (`[mcp_servers]` in `config.toml`), so it can use the same Node bridge as every other Mnemo integration. But there is a second, equally load-bearing half: **`AGENTS.md`** — Codex's native standing-instructions file. Tools without instructions are not enough (see [Why AGENTS.md matters](#why-agentsmd-matters)).

## Prerequisites

- **Codex CLI** with MCP support (`codex mcp list` works)
- **Node.js 18+** on your PATH (for the bridge)
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md). Local (`http://localhost:50001`) or anywhere reachable on your network.

## Install

### 1. Clone the bridge

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/mcp-bridge
npm install
```

### 2. Add the MCP server to `config.toml`

Open `~/.codex/config.toml` (`%USERPROFILE%\.codex\config.toml` on Windows) and add:

```toml
[mcp_servers.mnemo-cortex]
command = "node"
args = ["/path/to/mnemo-cortex/integrations/mcp-bridge/server.js"]
env = { MNEMO_URL = "http://localhost:50001", MNEMO_AGENT_ID = "coder", BRAIN_DIR = "/path/to/your-brain-repo/brain" }
```

- `MNEMO_AGENT_ID` — this agent's identity in the memory system (lowercase, stable). Every memory it saves and recalls is tagged with this id.
- `BRAIN_DIR` — optional; points at a brain directory (a git repo of shared markdown files) if you run one. Omit for memory-only use.

Verify with `codex mcp list` — the server should show as `enabled`.

### 3. Install the standing orders (`AGENTS.md`)

Copy [`AGENTS.md`](AGENTS.md) from this directory to `~/.codex/AGENTS.md` and replace the `<placeholders>` (agent id, paths, server URL). Codex injects this file into **every** session, from any working directory — it is the equivalent of Claude Code's `CLAUDE.md`.

That's the whole install.

## Why AGENTS.md matters

An MCP tool the model *can* call is not a routine the model *will* run. A fresh session has no memory of how it is supposed to boot — if nothing tells it "call `agent_startup` first," a capable model will improvise: hunt the filesystem for startup scripts, find another agent's, and run them under the wrong identity. (Field-tested: we watched a coding agent on a shared machine discover a *different* agent's startup scripts on disk and boot as that agent, polluting the shared memory store.)

The discipline that makes memory work — boot at session start, write back at session end — has to live in the harness's native instruction file, where it is injected into every session automatically:

| Harness | Instruction file |
|---|---|
| Codex CLI | `~/.codex/AGENTS.md` |
| Claude Code | `CLAUDE.md` |
| Claude Desktop | Project/profile instructions |
| OpenClaw / Hermes | Workspace instructions |
| LM Studio | System prompt |

With the instructions in place, the model's intelligence stops being a prerequisite and becomes headroom: a small local model follows the ritual because it is written down; a frontier model follows it and improves on it. That is the point — the boot/write-back ritual plus the memory server is a **Cortex OS**: an operating layer any model can run, not a habit only clever models develop.

## Gotchas

- **Two halves, both required.** MCP block without AGENTS.md = tools nobody calls. AGENTS.md without the MCP block = instructions pointing at missing tools (the model may then improvise raw HTTP calls — worse).
- **Identity is per-call, not ambient.** If the model ever falls back to raw HTTP against the Mnemo server, it must pass the same agent id the bridge uses. Spell the fallback out in AGENTS.md, or forbid it.
- **Windows encoding.** Keep `AGENTS.md` ASCII or UTF-8; if you see garbled punctuation through PowerShell, that's console rendering, not file corruption — but pure ASCII avoids all doubt.
