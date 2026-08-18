# Use Mnemo Cortex With Any Local LLM

> Split out of the main README to keep it readable — this is the complete host-by-host setup guide.

> Run any local LLM. Add Mnemo for memory. **No cloud, no subscription, no API keys for the model. Free forever.**

Mnemo Cortex talks Model Context Protocol (MCP). Every modern local-LLM host either supports MCP natively or has a one-line bridge. Pick your host and follow the snippet below.

> **Why this matters.** Zapier's "AI tool connections" run **$20–50/month** per workflow. Same pattern with Mnemo + your local LLM: **$0/mo, fully private, runs on hardware you already own.**

### Prerequisites (once)

1. **Run Mnemo Cortex** — locally, in Docker, or on a network box. The bridge is just an HTTP client; the server can be anywhere reachable. See the [Install Guide](#install-guide).
2. **Clone this repo** somewhere your LLM host can reach:
   ```
   git clone https://github.com/GuyMannDude/mnemo-cortex.git
   cd mnemo-cortex/integrations/mcp-bridge && npm install
   ```
   That's the bridge. It's a small Node script. Every host below points at the same `server.js`.

The full path to `server.js` and your Mnemo URL go into each host's config below.

---

### LM Studio — native MCP, GUI

> 📖 **Full install guide with troubleshooting:** [`integrations/lmstudio/`](integrations/lmstudio/)

LM Studio added native MCP support in v0.3.17. Edit `mcp.json` and restart.

**Config path:**
- Windows: `%USERPROFILE%\.lmstudio\mcp.json`
- macOS / Linux: `~/.lmstudio/mcp.json`

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/mcp-bridge/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "lmstudio"
      }
    }
  }
}
```

Restart LM Studio. Open a chat with a tool-capable model (Qwen3, Llama 3.2, Mistral). Click the **MCP** tab in the chat panel — `mnemo-cortex` should be listed with **13 tools** (4 memory + 4 facts + 5 Passport). Ask "save a note that I prefer concise replies" — the model calls `mnemo_save`. New chat: "what do you remember about my preferences?" — the model calls `mnemo_recall`.

---

### Open WebUI — native MCP, multi-model

Open WebUI works with any backend (Ollama, llama.cpp, OpenAI-compatible). In **Settings → Tools → MCP Servers**, add a stdio server:

| Field | Value |
|---|---|
| Name | `mnemo-cortex` |
| Command | `node` |
| Args | `/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/mcp-bridge/server.js` |
| Env | `MNEMO_URL=http://localhost:50001`<br>`MNEMO_AGENT_ID=open-webui` |

Save. Open a chat. Tools appear inline.

---

### AnythingLLM — desktop GUI, multi-workspace

> 📖 **Full install guide with verified gotchas:** [`integrations/anythingllm/`](integrations/anythingllm/)

AnythingLLM speaks MCP through its plugin layer. Two setup steps: drop in the MCP config, then flip the workspace to **Automatic mode** so memory tools fire on every message without a manual prefix.

**1. MCP config — edit `anythingllm_mcp_servers.json`:**

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\anythingllm-desktop\storage\plugins\anythingllm_mcp_servers.json` |
| macOS | `~/Library/Application Support/anythingllm-desktop/storage/plugins/anythingllm_mcp_servers.json` |
| Linux | `~/.config/anythingllm-desktop/storage/plugins/anythingllm_mcp_servers.json` |

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/mcp-bridge/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "anythingllm"
      }
    }
  }
}
```

**2. Flip the workspace to Automatic mode:** Open the workspace → ⚙️ Settings → **Chat Settings** tab → change mode to **Automatic**.

Per [AnythingLLM's docs](https://docs.anythingllm.com/features/chat-modes), Automatic mode "automatically uses all available agent-skills, tools, and MCPs." That means `mnemo_save` and `mnemo_recall` fire whenever the model decides they're useful — no `@agent` prefix, just normal conversation.

> **Visual cue:** if the chat input shows an `@` symbol on the left, you're still in the default mode and need to type `@agent` per message. If it's gone, Automatic mode is on and memory just works.

**Fallback:** if your workspace can't run Automatic mode (model doesn't support native tool calling, etc.), you can stay in default mode and prefix tool-using messages with `@agent`:
> `@agent please save a memory using mnemo_save: I prefer concise replies.`

**Three real gotchas (verified 2026-04-27 on a Windows 11 box):**

1. **Use a tool-capable model.** `qwen3:8b` and similar **do** invoke `mnemo_save` correctly. `llama3.1:8b` *narrates* "saved with id e4d3c9..." while never calling the tool — the memory ID is hallucinated. We tested both. Same bridge, same server, just a different model. Stick with qwen3.
2. **Verify the actual model.** AnythingLLM's GUI may show one model name while `.env` (`%APPDATA%\anythingllm-desktop\storage\.env`) retains a stale `OLLAMA_MODEL_PREF`. Restart fully after switching models.
3. **Verify the Ollama URL.** `OLLAMA_BASE_PATH` in `.env` may auto-discover a network Ollama that doesn't have your model. Set it to `http://localhost:11434` if your model lives on the same machine.

---

### llama.cpp — native MCP

`llama-server` ships with MCP client support. Run with `--mcp-config`:

```bash
llama-server \
  -m qwen3-8b.gguf \
  --mcp-config /path/to/mcp.json
```

Use the same `mcp.json` shape as LM Studio above.

---

### Ollama — via MCPHost or ollmcp

Ollama has no native MCP support yet ([issue #7865](https://github.com/ollama/ollama/issues/7865)). Use a bridge:

**Option 1 — MCPHost** (Go binary, multi-platform):

```bash
go install github.com/mark3labs/mcphost@latest
# OR download a Windows binary from https://github.com/mark3labs/mcphost/releases
```

```yaml
# ~/.mcphost.yaml
mcpServers:
  mnemo-cortex:
    type: local
    command:
      - "node"
      - "/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/mcp-bridge/server.js"
    environment:
      MNEMO_URL: "http://localhost:50001"
      MNEMO_AGENT_ID: "ollama-mcphost"
model: "ollama:qwen3:8b"
```

```bash
mcphost                                    # interactive
mcphost -p "save a note about X" --quiet   # scripted
```

**Option 2 — ollmcp** (Python TUI):

```bash
pip install mcp-client-for-ollama
ollmcp
```

> **Heads-up for Windows users:** MCPHost's interactive UI must run in a real console window. Driving it through SSH-stdio doesn't work — Windows buffers the output until the process exits. Run it locally on the box where Ollama lives.

---

### LobeChat — MCP plugin

In **Settings → Plugins → MCP → Add custom MCP server**:

| Field | Value |
|---|---|
| Type | `stdio` |
| Command | `node /ABSOLUTE/PATH/TO/mnemo-cortex/integrations/mcp-bridge/server.js` |
| Env | `MNEMO_URL=http://localhost:50001`<br>`MNEMO_AGENT_ID=lobechat` |

---

### Jan — MCP via extensions

Jan exposes MCP through its Extensions panel. **Settings → Extensions → MCP Servers → Add**:

```json
{
  "name": "mnemo-cortex",
  "command": "node",
  "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/mcp-bridge/server.js"],
  "env": {
    "MNEMO_URL": "http://localhost:50001",
    "MNEMO_AGENT_ID": "jan"
  }
}
```

Restart Jan. Tools appear in the assistant configuration.

---

### What you get

By default, **13 tools** that work for any user:

| Group | Tools |
|---|---|
| Memory | `mnemo_recall`, `mnemo_search`, `mnemo_save`, `mnemo_share` |
| Facts | `mnemo_fact_get`, `mnemo_fact_query`, `mnemo_fact_save`, `mnemo_fact_demote` |
| [Developer's Passport](passport/) | `passport_get_user_context`, `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override` |

The bridge also detects two optional dirs and registers more tools when present:

- Set `BRAIN_DIR` to a brain-file checkout (use the [mnemo-plan template](https://github.com/GuyMannDude/mnemo-plan) for a clean starting point) → adds `agent_startup`, `opie_startup`, `read_brain_file`, `list_brain_files`, `write_brain_file`, `session_end`.
- Set `WIKI_DIR` to a static wiki dir → adds `wiki_search`, `wiki_read`, `wiki_index` (legacy WikAI pages — see **The Librarian** above for what replaced the wiki as the discovery system).

If the directory doesn't exist, those tools simply don't register — the model never sees them. Most users stay on the 13-tool default and that's the right call.

| Setup | Tools |
|---|---|
| Default (any user) | **13** |
| + brain dir | 19 |
| + wiki dir | 16 |
| Both | 22 |

### Tips

- **Pick a tool-capable model.** Qwen3, Llama 3.2, Mistral, and Gemma 2 all do tool-calling well. Smaller models (under 7B) can struggle; if the model never invokes the tool, scale up.
- **First call is slow.** Cold model load + tool round-trip can take 30–60s. After the model is warm, calls are sub-second.
- **`MNEMO_AGENT_ID` matters.** Each host should use a distinct agent ID (`lmstudio`, `ollama`, `jan`, etc.) so memories don't collide. If you're using Mnemo's cross-agent dreaming feature, the agent ID is what shows up in the dream brief.

### More on hosts, models, and what actually works

For host-by-host pass/fail, model tool-calling test results, browser automation comparisons, and the rest of our field findings: **[projectsparks.ai/field-guide](https://projectsparks.ai/field-guide)**. Updated as we test more.

---

### The Memory Architecture

**Three layers, one source of truth:**

| Layer | Role | Analogy |
|---|---|---|
| **Mnemo Cortex** | Source of truth. Raw facts, sessions, key events. Multi-agent, query-time. | The librarian's filing cabinet |
| **The Librarian** | Discovery index. SQLite FTS5 over every file in the workspace. Turns "the file about X" into a path. | The card catalog |
| **Brain files** | Live working memory. Current state, identity, active context per agent. | The sticky notes on your desk |

**When they disagree, Mnemo wins.** The Librarian's index is always rebuildable from the filesystem. Brain files are ephemeral. This split is what lets the system scale: facts go where they're addressable (Mnemo), documents go where they're findable (the Librarian), and active state stays where it can change at the speed of work (brain files).

**Embedding fallback.** Embeddings default to local Ollama (`nomic-embed-text`) for zero-cost, zero-latency operation. If Ollama is unreachable, Mnemo falls back to hosted Google Gemini embeddings (Matryoshka-truncated to match the 768-dim store width). The free tier covers any plausible outage window, giving you graceful degradation at effectively zero cost.

---

### Inspirations

We did not invent this. We adopted the best ideas in the air, credited them openly, and built on top.

- **[Andrej Karpathy's LLM Wiki](https://gist.github.com/karpathy)** (April 2026, 41,000+ bookmarks) — the pattern of compiling AI understanding into navigable artifacts instead of rederiving from raw data on every query. WikAI was our implementation of this pattern — since retired in favor of the Librarian's index-don't-compile approach, but the credit stands. Also the "idea file as publishing format" approach we use in `SETUP-PROMPT.md`.
- **Nate B Jones — [OpenBrain](https://github.com/NateBJones-Projects/OB1) and [the analysis video](https://youtu.be/dxq7WtWxi44)** ([Substack](https://natesnewsletter.substack.com/), [YouTube](https://www.youtube.com/@NateBJones)) — the write-time vs query-time fork, and the hybrid architecture: structured data as source of truth, compiled wiki as the browsable layer over the top. Our original three-layer architecture mapped directly to Nate's hybrid model; the compiled-wiki layer has since given way to the Librarian's index, but the source-of-truth-plus-browsable-layer split came from here.
- **[Google A2A Protocol](https://github.com/google/A2A)** — agent-to-agent standard. Sparks Bus speaks A2A's data shapes today; transport is the v2 roadmap.
- **[Mem0](https://mem0.ai)** — the first to make portable AI memory feel real. Inspired our early thinking about cross-session persistence.

---

### *A Crustacean That Never Forgets* 🧠🦞

The full stack:

```
                    ┌─────────────────────────────────────────┐
                    │           Mnemo Cortex Stack            │
                    └─────────────────────────────────────────┘

  Agents (any MCP-capable agent — name them yourself)
    │                                     ┌──────────────┐
    ├── recall / save / search ──────────▶│ Mnemo SQLite │ ◀── Source of Truth
    │                                     │  + FTS5 +    │
    │                                     │  Embeddings  │
    │                                     └──────┬───────┘
    │                                            │
    ├── bus_send / bus_read / bus_reply ──▶ Sparks Bus ──▶ Discord (#dispatch)
    │                                      (SQLite)        📬 → ✅ → 🔄
    │
    ├── file_find (FrankenClaw) ─────────▶ Librarian (SQLite FTS5, 107K files)
    │                                       ▲
    │                                       │ reindexed nightly
    │                                       │
    │                              ┌────────┴───────┐
    │                              │  Nightly jobs  │ 3:15 AM → Dream Brief
    │                              │  Dreaming +    │ 3:40 AM → Reindex
    │                              │  Librarian     │
    │                              └────────────────┘
    │
    └── passport_* ──────────────────────▶ Passport (user prefs)
```
