# Mnemo Cortex MCP Bridge

The generic Node.js MCP server that every Mnemo Cortex integration spawns. Drop the right `command` + `args` into your MCP client config and your agent gets a tool list backed by Mnemo Cortex.

This is the shared bridge — **claude-desktop**, **claude-code**, **openclaw-mcp**, **lmstudio**, **anythingllm**, **agent-zero**, **hermes**, **ollama-desktop** all point at this same `server.js`. Each integration directory adds a thin host-specific wiring layer (install script + config example + README); the actual bridge logic lives here.

## Tools the bridge exposes

- `mnemo_recall` — semantic recall scoped to the calling agent, with v3 provenance/decay filters and structured `stale_warning` on aged records.
- `mnemo_save` — write a memory with optional `source` / `category` / `additional_tags`. Regex auto-suggester picks a category when omitted and returns its choice + matched keywords.
- `mnemo_search` — cross-agent recall (gated by share mode), same filter surface as `mnemo_recall`.
- `mnemo_share` — toggle cross-agent sharing for the current session.
- `agent_startup` — neutral session-boot tool that loads the calling agent's lane file, cross-agent docs, recent memories, and last dream brief.
- `session_end` — drains auto-capture, saves a session-summary memory, commits the agent's brain lane.
- `wiki_search` / `wiki_read` / `wiki_index` — query static wiki pages, a legacy WikAI archive (when `WIKI_DIR` is configured). No longer the primary discovery system — that's the Librarian (`librarian.py` in the main repo; see the README's Librarian section).
- `read_brain_file` / `write_brain_file` / `list_brain_files` — read/write the agent's brain repo (when `BRAIN_DIR` is configured).
- `passport_observe_behavior` / `passport_get_user_context` — Developer's Passport pipeline.

The brain-lane, wiki, and Passport tools only register when their backing directories exist — new users without those checkouts get a clean memory bridge; operators with a brain or wiki dir get the full kit automatically.

## Prerequisites

- **Node.js 18+** (native fetch + ESM modules)
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md)
- (Optional) **MCP-capable host** — Claude Desktop, Claude Code, OpenClaw, LM Studio, AnythingLLM, Agent Zero, Hermes Agent, Open WebUI, llama.cpp, LobeChat, Jan, Ollama (via MCPHost / ollmcp)

## Install

```bash
cd mnemo-cortex/integrations/mcp-bridge
npm ci
```

## Point your MCP host at it

The exact config syntax varies by host — see the host-specific README in the sibling integration directory. The common shape:

```json
{
  "command": "node",
  "args": ["/absolute/path/to/mnemo-cortex/integrations/mcp-bridge/server.js"],
  "env": {
    "MNEMO_URL": "http://localhost:50001",
    "MNEMO_AGENT_ID": "your-agent-name"
  }
}
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Where your Mnemo Cortex server is reachable. |
| `MNEMO_AGENT_ID` | `openclaw` | Identifies this client to Mnemo. Memories are scoped per agent — pick a unique name. |
| `MNEMO_SHARE` | `separate` | Cross-agent memory sharing. One of `separate` (default, see only your own memories), `always` (read other agents' memories via explicit `agent_id`; unscoped search stays self-scoped), `never` (block even `mnemo_share` toggle). |
| `BRAIN_DIR` | `~/mnemo-plan/brain` | Optional. Brain repo for `read_brain_file` / `write_brain_file` / lane-aware boot. Tools skip silently when the dir doesn't exist. |
| `WIKI_DIR` | `~/wiki` | Optional. Wiki dir for `wiki_*` tools. Tools skip silently when the dir doesn't exist. |
| `DREAM_DIR` | `~/.agentb/dreams` | Optional. Dream brief location for `agent_startup` to surface in session boot. |

## Heads-up: old path is back-compat only

If you have MCP config from before 2.8.1 pointing at `integrations/openclaw-mcp/server.js` — it still works via a symlink, but please update the path to `integrations/mcp-bridge/server.js` when convenient. The old path will be removed in a future major version. See `../openclaw-mcp/README.md` for the deprecation notice.

## See also

- [Main Mnemo Cortex README](../../README.md) — full install guide, integrations index, architecture.
- [CHANGELOG.md](CHANGELOG.md) — release history for this bridge.
- Host-specific install guides: `../claude-desktop/`, `../claude-code/`, `../openclaw-mcp/`, `../lmstudio/`, `../anythingllm/`, `../agent-zero/`, `../hermes/`, `../ollama-desktop/`.
