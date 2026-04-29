<p align="center">
  <img src="docs/mnemo-cortex-constellation.png" alt="Mnemo Cortex constellation — verified hosts: Claude Desktop, LM Studio, AnythingLLM, OpenClaw, Agent Zero, Ollama. Local-first, cross-agent, open source. A Mnemo in Every Bot." width="540">
</p>

# ⚡ Mnemo Cortex v2.6.4

![GitHub stars](https://img.shields.io/github/stars/GuyMannDude/mnemo-cortex)
![License](https://img.shields.io/github/license/GuyMannDude/mnemo-cortex)

## Memory That Dreams, Compiles, and Connects

> Every AI agent has amnesia. Mnemo Cortex fixes that — and then some.
> Persistent memory that survives across sessions, searches by meaning, and costs $0 to run.

| | |
|---|---|
| 🧠 **Deep Recall** | Persistent memory across sessions. Semantic search. $0 to run. |
| 🌙 **Dreaming** | Cross-agent overnight synthesis. Every agent wakes up knowing what the others did. |
| 📚 **WikAI** | Auto-compiled knowledge base. The wiki is regenerated nightly from Mnemo. Never goes stale. |
| 📬 **Sparks Bus** | Agent-to-agent messaging with delivery confirmation. A2A-compatible. |
| 🪪 **Developer's Passport** | Safe behavioral-claim ingestion layer. Review queue + 32 detectors + provenance buckets. Dev-targeted beta. |
| 🔗 **Mem0 Bridge** | "And Mem0, not instead of Mem0." Use both. |

### 🚀 Get Started

⌘ **[Claude Code → 60-second install](integrations/claude-code/)** — Give CC Fluid Memory with Deep Recall

🖥️ **[Claude Desktop → one-click `.mcpb` bundle](integrations/claude-desktop/)** — Drag-and-drop install. No clone, no Node, no JSON editing. Works on Windows, macOS, and Linux.

🦞 **[OpenClaw → MCP integration](integrations/openclaw-mcp/)** — Give Your ClawdBot a Brain. One Config Line.

🎛️ **[LM Studio → native MCP, GUI](integrations/lmstudio/)** — `mcp.json` + restart. Works with any tool-capable open-weights model.

📦 **[AnythingLLM → desktop GUI, multi-workspace](integrations/anythingllm/)** — Drop-in MCP config + Automatic mode. No `@agent` prefix needed.

🤖 **[Agent Zero → autonomous Docker agents](integrations/agent-zero/)** — In-container MCP setup. Cross-agent memory between research, courier, code-exec bots.

🦣 **[Ollama Desktop → terminal `ollama launch openclaw`](integrations/ollama-desktop/)** — Ollama as the local LLM, OpenClaw as the MCP host. Note: Ollama Desktop's *own chat window* doesn't support MCP — use the terminal launcher.

🦙 **[Any Local LLM → MCP setup](#use-with-any-local-llm)** — Open WebUI, llama.cpp, Ollama, LobeChat, Jan, and more

🧭 **[How should my agent use it? → Session Guide](SESSION-GUIDE.md)** — Workflow patterns, per-platform boot snippets, common mistakes

---

### 📜 How to Use Mnemo Effectively

Read **[THE-LANE-PROTOCOL.md](THE-LANE-PROTOCOL.md)** — the operating practice for running agents with persistent memory. Feed it to your agent or follow it yourself. It takes 5 minutes per session and makes every cold start feel warm.

The protocol pairs with this product the way a recipe pairs with ingredients: Mnemo gives you the memory store, the Lane Protocol gives you the loop that makes it pay off. Distilled from real multi-agent sessions — terminal agents, chat agents, and autonomous workers running the same six-step ritual.

---

### Dreaming Mnemo — Cross-Agent Overnight Synthesis

Every night, Mnemo reads every connected agent's memories and synthesizes them into a single brief. Each agent wakes up knowing what the others did. No manual relay. No copy-paste. It just happens.

**This is the only AI memory system that does cross-agent synthesis.** Mem0, Zep, and Letta store memory per agent. Mnemo dreams across all of them.

### Works with Mem0

Already using Mem0? Keep it. Mnemo runs as a fast local working-memory layer in front of your existing Mem0 deployment. When Mnemo has what you need: sub-100ms local recall. When local results are thin: automatic fallback to Mem0 for depth. Writes sync both ways.

**"And Mem0" — not "instead of Mem0."**

### Deploy Your Way

- **Shared** — One Mnemo for all agents. Cross-agent search and dreaming. Full team awareness.
- **Isolated** — Separate Mnemo per agent or per customer. Zero bleed between tenants.
- **Hybrid** — Shared for internal agents + isolated for customer-facing bots. This is what we run.

Mem0 makes you choose one shared store. Mnemo lets you architect for your actual privacy and separation needs.

---

### 📚 WikAI — Compiled Knowledge Base

A 3,000+ page wiki layer auto-compiled from Mnemo data. Organized into `projects/`, `entities/`, `concepts/`, and `sources/`. Searchable through three MCP tools: `wiki_search`, `wiki_read`, `wiki_index`.

**The wiki is never edited directly.** It's recompiled nightly by [`mnemo-wiki-compile.py`](mnemo-wiki-compile.py) from Mnemo data. Mnemo is the source of truth. The wiki is the study guide. If a page is wrong, fix the source memories in Mnemo and recompile.

The compiler clusters recent memories by topic, passes each cluster + the existing page to gemini-2.5-flash, and writes a fully-rewritten page that integrates the new information without bloating. Cross-references are validated against the live page set — no hallucinated wikilinks. Every page carries a provenance footer listing the Mnemo session IDs that fed it, so any claim is auditable. Per-page failures are isolated; one bad LLM call posts ⚠️ to `#alerts` and the run continues.

This is the **Karpathy/Nate Jones hybrid** in production: query-time facts in Mnemo + write-time synthesis in WikAI. Neither Mem0, Zep, nor Letta offer this. See [Inspirations](#inspirations) below.

---

### 📬 Sparks Bus — Agent-to-Agent Messaging

A delivery-confirmed messaging system for multi-agent communication. Lives as a module inside Mnemo Cortex at [`sparks_bus/`](sparks_bus/) AND ships standalone at [github.com/GuyMannDude/sparks-bus](https://github.com/GuyMannDude/sparks-bus).

**Doctrine:** Discord is the doorbell. Mnemo is the mailbox. The tracking ID is the receipt.

**Lifecycle visible in `#dispatch`:**
```
📬 DELIVERED  →  ✅ PICKED UP  →  🔄 LOOP CLOSED
```
Plus one-shot ⚠️ alerts in `#alerts` for delivery failures and stale messages. No retry storms.

**Two install modes auto-detected at startup:**
- **Full** — Mnemo reachable. Payload saved to Mnemo by tracking ID. Discord notifications carry just the receipt.
- **Standalone** — No Mnemo. Payload travels in the Discord notification itself. Same lifecycle, no semantic recall.

**A2A compatible.** Agent Cards live in [`sparks_bus/agent-cards/`](sparks_bus/agent-cards/) for every agent in the deployment, formatted to [Google's A2A spec](https://github.com/google/A2A). Each bus message maps to an A2A Task: `tracking_id → task.id`, `subject → task.name`, `body → task.input`, lifecycle → A2A `TaskState`. Transport (HTTPS / JSON-RPC) is the v2 roadmap; data shape compatibility is in now. See [`sparks_bus/A2A.md`](sparks_bus/A2A.md).

**Includes [`SETUP-PROMPT.md`](sparks_bus/SETUP-PROMPT.md)** — a self-contained prompt any AI agent can read to bootstrap the entire bus on a fresh deployment. Karpathy's "idea file as publishing format" pattern.

---

### 📋 mnemo-plan — Project Pad for Your Agents

Mnemo Cortex captures conversation memory automatically. **mnemo-plan** is the manual companion: a folder of markdown files in Git that you write and curate, and any LLM agent can read at session start via the Mnemo MCP tools.

The split:

- **Mnemo Cortex** = automatic conversation memory (save / recall / search happens in the background as agents work)
- **mnemo-plan** = manual project pad (you write it, agents read it — project specs, active tasks, decision logs, architecture notes)

Same MCP bridge handles both. mnemo-plan tools (`read_brain_file`, `write_brain_file`, `list_brain_files`, plus `opie_startup` and `session_end`) auto-enable when `BRAIN_DIR` is set on disk; if there's no plan repo, those tools simply don't register.

The starter template repo: [github.com/GuyMannDude/mnemo-plan](https://github.com/GuyMannDude/mnemo-plan). Fork it, fill in your project's files, point `BRAIN_DIR` at it. Your agents now have project context the moment they start a session — without you re-explaining your setup every time.

---

### 🪪 Developer's Passport — Safe Behavioral-Claim Ingestion

**Status: beta. Dev-targeted release.** A reference-grade safety layer for developers building agent systems that need to ingest user working-style claims into an agent's context. Observations are recorded as candidates, reviewed, and promoted to stable claims; nothing lands in the user's profile without an explicit promotion step.

What's in the box: 5 MCP tools, a review queue, 32 content detectors (secrets, PII, prompt injection, generic fluff, duplicates), 4 provenance buckets, a policy layer with 4-way disposition outcomes, git-tracked audit, and a 200-entry eval corpus. Current eval: 53.0% accuracy / 0.458 macro-F1.

MCP tools: `passport_get_user_context`, `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override`. Reference integration via stdio MCP at [`integrations/openclaw-mcp/`](integrations/openclaw-mcp/). See [`passport/README.md`](passport/README.md) for the 5-minute quickstart.

Designed so the user owns the artifact, not the platform. The possessive in the name is deliberate — it drops when the hosted / browser-AI release for normal users ships. Today's release is for devs who wire MCP subprocesses into their own agent stacks.

---

## 🦙 Use With Any Local LLM

> Run any local LLM. Add Mnemo for memory. **No cloud, no subscription, no API keys for the model. Free forever.**

Mnemo Cortex talks Model Context Protocol (MCP). Every modern local-LLM host either supports MCP natively or has a one-line bridge. Pick your host and follow the snippet below.

> **Why this matters.** Zapier's "AI tool connections" run **$20–50/month** per workflow. Same pattern with Mnemo + your local LLM: **$0/mo, fully private, runs on hardware you already own.**

### Prerequisites (once)

1. **Run Mnemo Cortex** — locally, in Docker, or on a network box. The bridge is just an HTTP client; the server can be anywhere reachable. See the [Install Guide](#install-guide).
2. **Clone this repo** somewhere your LLM host can reach:
   ```
   git clone https://github.com/GuyMannDude/mnemo-cortex.git
   cd mnemo-cortex/integrations/openclaw-mcp && npm install
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
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "lmstudio"
      }
    }
  }
}
```

Restart LM Studio. Open a chat with a tool-capable model (Qwen3, Llama 3.2, Mistral). Click the **MCP** tab in the chat panel — `mnemo-cortex` should be listed with **9 tools** (4 memory + 5 Passport). Ask "save a note that I prefer concise replies" — the model calls `mnemo_save`. New chat: "what do you remember about my preferences?" — the model calls `mnemo_recall`.

---

### Open WebUI — native MCP, multi-model

Open WebUI works with any backend (Ollama, llama.cpp, OpenAI-compatible). In **Settings → Tools → MCP Servers**, add a stdio server:

| Field | Value |
|---|---|
| Name | `mnemo-cortex` |
| Command | `node` |
| Args | `/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js` |
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
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"],
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

**Three real gotchas (verified 2026-04-27 on IGOR-2):**

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
      - "/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"
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
| Command | `node /ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js` |
| Env | `MNEMO_URL=http://localhost:50001`<br>`MNEMO_AGENT_ID=lobechat` |

---

### Jan — MCP via extensions

Jan exposes MCP through its Extensions panel. **Settings → Extensions → MCP Servers → Add**:

```json
{
  "name": "mnemo-cortex",
  "command": "node",
  "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"],
  "env": {
    "MNEMO_URL": "http://localhost:50001",
    "MNEMO_AGENT_ID": "jan"
  }
}
```

Restart Jan. Tools appear in the assistant configuration.

---

### What you get

By default, **9 tools** that work for any user:

| Group | Tools |
|---|---|
| Memory | `mnemo_recall`, `mnemo_search`, `mnemo_save`, `mnemo_share` |
| [Developer's Passport](passport/) | `passport_get_user_context`, `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override` |

The bridge also detects two optional dirs and registers more tools when present:

- Set `BRAIN_DIR` to a brain-lane checkout (use the [mnemo-plan template](https://github.com/GuyMannDude/mnemo-plan) for a clean starting point) → adds `opie_startup`, `read_brain_file`, `list_brain_files`, `write_brain_file`, `session_end`.
- Set `WIKI_DIR` to a wiki dir → adds `wiki_search`, `wiki_read`, `wiki_index`.

If the directory doesn't exist, those tools simply don't register — the model never sees them. Most users stay on the 9-tool default and that's the right call.

| Setup | Tools |
|---|---|
| Default (any user) | **9** |
| + brain dir | 14 |
| + wiki dir | 12 |
| Both | 17 |

Pair with [FrankenClaw](https://github.com/GuyMannDude/frankenclaw) for web search, vision, browser, NotebookLM, Shopify, and Google Drive tools. Same MCP config pattern — just add a second `mcpServers` entry.

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
| **WikAI** | Compiled view. Auto-generated from Mnemo. Cross-referenced, browsable. Write-time. | The study guide |
| **Brain files** | Live working memory. Current state, identity, active context per agent. | The sticky notes on your desk |

**When they disagree, Mnemo wins.** WikAI is always regenerable from Mnemo. Brain files are ephemeral. This split is what lets the system scale: facts go where they're addressable (Mnemo), synthesis goes where it's browsable (WikAI), and active state stays where it can change at the speed of work (brain files).

---

### Inspirations

We did not invent this. We adopted the best ideas in the air, credited them openly, and built on top.

- **[Andrej Karpathy's LLM Wiki](https://gist.github.com/karpathy)** (April 2026, 41,000+ bookmarks) — the pattern of compiling AI understanding into navigable artifacts instead of rederiving from raw data on every query. WikAI is our implementation of this pattern. Also the "idea file as publishing format" approach we use in `SETUP-PROMPT.md`.
- **Nate B Jones — [OpenBrain](https://github.com/NateBJones-Projects/OB1) and [the analysis video](https://youtu.be/dxq7WtWxi44)** ([Substack](https://natesnewsletter.substack.com/), [YouTube](https://www.youtube.com/@NateBJones)) — the write-time vs query-time fork, and the hybrid architecture: structured data as source of truth, compiled wiki as the browsable layer over the top. Our three-layer architecture maps directly to Nate's hybrid model.
- **[Google A2A Protocol](https://github.com/google/A2A)** — agent-to-agent standard. Sparks Bus speaks A2A's data shapes today; transport is the v2 roadmap.
- **[Mem0](https://mem0.ai)** — the first to make portable AI memory feel real. Our Mem0 Bridge is "and Mem0, not instead of Mem0."

---

### *A Crustacean That Never Forgets* 🧠🦞

Proven on two live agents — Rocky with six weeks of recall, Alice with one.

```
OpenClaw Agent ──writes──▶ Session Tape (disk)
                                │
                          Watcher Daemon ──reads──▶ Mnemo v2 SQLite
                                                        │
                          Refresher Daemon ◀──reads─────┘
                                │
                          writes──▶ MNEMO-CONTEXT.md ──▶ Agent Bootstrap
```

The full v2.4 stack:

```
                    ┌─────────────────────────────────────────┐
                    │           Mnemo Cortex Stack            │
                    └─────────────────────────────────────────┘

  Agents (CC, Rocky, Opie, BW, Cliff)
    │                                     ┌──────────────┐
    ├── recall / save / search ──────────▶│ Mnemo SQLite │ ◀── Source of Truth
    │                                     │  + FTS5 +    │
    │                                     │  Embeddings  │
    │                                     └──────┬───────┘
    │                                            │
    ├── bus_send / bus_read / bus_reply ──▶ Sparks Bus ──▶ Discord (#dispatch)
    │                                      (SQLite)        📬 → ✅ → 🔄
    │
    ├── wiki_search / wiki_read ─────────▶ WikAI (3,000+ .md pages)
    │                                       ▲
    │                                       │ auto-compiled nightly
    │                                       │
    │                              ┌────────┴───────┐
    │                              │  Dreaming      │ 3:15 AM → Dream Brief
    │                              │  + Wiki        │ 3:30 AM → Wiki Pages
    │                              │  Compiler      │
    │                              └────────────────┘
    │
    ├── passport_* ──────────────────────▶ Passport (user prefs)
    │
    └── Mem0 Bridge ─────────────────────▶ Mem0 (fallback depth layer)
```

## Health Monitoring

Built-in deployment verification. No agent runs without verified memory.

```
mnemo-cortex health
```

```
mnemo-cortex health check
=========================

Core Services
  API server (http://artforge:50001) ...... OK (v2.1.0, 156 memories, 42ms)
  Database ................................. OK (12 sessions (3 hot, 4 warm, 5 cold))
  Compaction model ......................... OK (qwen2.5:32b-instruct — responding)

Agents (3 discovered)
  rocky .................................... OK (recall returned 5 results (234ms))
  cc ....................................... OK (recall returned 3 results (189ms))
  opie ..................................... OK (recall returned 4 results (201ms))

Watchers
  mnemo-watcher-cc ......................... OK (active, PID 4521)
  mnemo-refresh ............................ OK (active, PID 4523)

MCP Registration
  openclaw.json ............................ OK (mnemo-cortex registered)

14/14 checks passed
```

Options: `--json` (machine-readable) · `--quiet` (exit code only) · `--agents` (agent checks only) · `--services` (watcher checks only) · `--check-mcp <path>` (validate MCP configs)

Wire to cron: `0 */6 * * * mnemo-cortex health --quiet || your-alert-command`

## Auto-Capture

Every agent conversation captured automatically. No manual saves, no hooks, no code changes.

### How It Works

Mnemo watches your agent's session files from the outside and ingests every message as it happens. Two adapter patterns depending on your agent platform:

| Platform | Capture Method | Command |
|----------|---------------|---------|
| **OpenClaw** | Session file watcher (tails JSONL) | `mnemo-cortex watch --backfill` |
| **Claude Code** | Session file watcher (same) | `mnemo-cortex watch --backfill` |
| **Claude Desktop** | MCP tools (save/recall/search) | [Setup guide](integrations/claude-desktop/) |

### Quick Start

```bash
# 1. Start Mnemo (if not already running)
mnemo-cortex start

# 2. Start auto-capture
mnemo-cortex watch --backfill
```

That's it. Every exchange your agent has is now captured, compressed, and searchable.

### Always-On Auto-Capture

Set the `MNEMO_AUTO_CAPTURE` environment variable to start the watcher automatically whenever Mnemo starts:

```bash
# Add to your shell profile (~/.bashrc, ~/.zshrc, etc.)
export MNEMO_AUTO_CAPTURE=true
```

With this set, `mnemo-cortex start` also starts the session watcher — no separate `watch` command needed.

### What Gets Captured

- Every user message and agent response
- Tool calls and results
- Session boundaries and timestamps
- All compressed via rolling compaction (80% token reduction, zero information loss on named entities)

### Verify It's Working

```bash
mnemo-cortex status
```

Look for:
```
  Watcher:    running (PID 4521) — auto-capturing sessions
```

Or check the database directly:
```bash
mnemo-cortex recall "what happened today"
```

---

## What It Does

Mnemo Cortex v2 is a **sidecar memory coprocessor** for AI agents. It watches your agent's session files from the outside, ingests every message into a local SQLite database, compresses older messages into summaries via LLM-backed compaction, and writes a `MNEMO-CONTEXT.md` file that your agent reads at bootstrap.

No hooks. No agent modifications. No cloud dependency. Mnemo keeps your memory on disk — if either process restarts, the data is already there.

## Key Features

- **SQLite + FTS5 storage** — Single database file. Full-text search. Zero dependencies beyond Python stdlib.
- **Context frontier with active compaction** — Rolling window of messages + summaries. 80% token compression while preserving perfect recall.
- **DAG-based summary lineage** — Every summary tracks its source messages via a directed acyclic graph. Expand any summary back to verbatim source.
- **Verbatim replay mode** — Compressed by default, original messages on demand.
- **OpenClaw session watcher daemon** — Tails JSONL session files and ingests new messages every 2 seconds.
- **Context refresher daemon** — Writes `MNEMO-CONTEXT.md` to the agent's workspace every 5 seconds.
- **Provider-backed summarization** — Compaction summaries generated by local Ollama (qwen2.5:32b-instruct) at $0. Any LLM provider supported as fallback.
- **Sidecar design** — Version-resistant. Observes from the outside. Never touches agent internals.

## Live Stats (March 2026)

Proven on two live OpenClaw agents:

| Agent | Host | Messages | Summaries | Conversations | Recall |
|-------|------|----------|-----------|---------------|--------|
| **Alice** | THE VAULT (Threadripper) | 210+ | 18+ | 5 | 1 week |
| **Rocky** | IGOR (laptop) | 3,000+ | 429+ | 20+ | 6 weeks |

## Install Guide

Five steps from a fresh checkout to a running server connected to your agent. The CLI handles everything — `mnemo-cortex init` writes the config, `mnemo-cortex start` launches the API server, `mnemo-cortex health` verifies, and you point your agent at it via the matching integration.

### Platforms

Mnemo Cortex runs on **Linux, macOS, and Windows**. The core (Python + SQLite) is cross-platform. Platform differences are mostly about how you keep the server running across reboots:

| | Linux | macOS | Windows |
|---|---|---|---|
| **Server lifecycle** | systemd / manual | launchd / manual | Task Scheduler / manual |
| **Claude Code** | Full support | Full support | Full support |
| **Claude Desktop** | `.mcpb` bundle | `.mcpb` bundle | `.mcpb` bundle |
| **OpenClaw** | Full support | Full support | Full support |

### Prerequisites

- **Python 3.11+** — for the server
- **Ollama** *(recommended)* — local reasoning + embedding models, free, fully private. The `init` wizard also accepts OpenAI / Google / Anthropic / OpenRouter API keys if you'd rather use a cloud model.
- **Node.js 18+** *(only when running an MCP-bridge integration: Claude Desktop, LM Studio, OpenClaw, etc.)*

### Step 1: Install

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

This registers two CLI commands: `mnemo-cortex` and the shorter alias `mnemo`.

### Step 2: Initialize

```bash
mnemo-cortex init
```

Interactive wizard. Picks your reasoning model (preflight checks), embedding model (semantic search), server bind address, port, and any agents you want to register up front. Defaults are sensible for a single-machine local install: Ollama for both models, `127.0.0.1:50001`, no auth token (loopback only).

The wizard writes the config to `~/.agentb/agentb.yaml` and creates the data directory at `~/.agentb/data/`.

### Step 3: Start the server

```bash
mnemo-cortex start                 # detached (logs go to ~/.agentb/data/logs/)
mnemo-cortex start --foreground    # attached, logs to terminal
```

Server listens on `http://localhost:50001` by default. To stop:

```bash
mnemo-cortex stop
```

### Step 4: Verify

Quick health check:

```bash
mnemo-cortex health
```

Deeper diagnostics — port conflicts, model availability, agent recall, watcher status:

```bash
mnemo-cortex doctor
```

Both return non-zero exit codes when something's broken, so they work in scripts and CI.

### Step 5: Connect an integration

The server is now running. Pick your platform and follow its integration guide:

| Host | Path |
|---|---|
| **Claude Code** | [`integrations/claude-code/`](integrations/claude-code/) — terminal agent, sync service |
| **Claude Desktop** | [`integrations/claude-desktop/`](integrations/claude-desktop/) — drag-and-drop `.mcpb` bundle |
| **LM Studio** | [`integrations/lmstudio/`](integrations/lmstudio/) — native MCP, GUI |
| **AnythingLLM** | [`integrations/anythingllm/`](integrations/anythingllm/) — desktop, multi-workspace |
| **OpenClaw** | [`integrations/openclaw-mcp/`](integrations/openclaw-mcp/) — one-line MCP config |
| **Agent Zero** | [`integrations/agent-zero/`](integrations/agent-zero/) — in-container Docker setup |
| **Ollama Desktop** | [`integrations/ollama-desktop/`](integrations/ollama-desktop/) — `ollama launch` flow |

Each integration is a one-line MCP config or a drag-and-drop bundle. The server is the same; only the bridge config changes.

For other MCP-capable hosts (Open WebUI, llama.cpp, LobeChat, Jan, generic MCP clients), see [Use With Any Local LLM](#-use-with-any-local-llm) above.

## Troubleshooting

**Recall / cross-agent search returns "No chunks"**

Most common cause: your embedding model setting doesn't match your provider's current model name. Model names change — check your provider's docs:

| Provider | Current Embedding Model | Deprecated / Dead |
|----------|------------------------|-------------------|
| **Ollama (local)** | `nomic-embed-text` | — |
| **OpenAI** | `text-embedding-3-small` | `text-embedding-ada-002` |
| **Google** | `gemini-embedding-001` | `text-embedding-004` (shut down Jan 2026) |

If you recently switched providers or updated your config, verify the model name is correct and that your API key has access to the embedding endpoint.

**Health check fails on "Compaction model"**

The compaction model (default: `qwen2.5:32b-instruct` via Ollama) must be running and reachable. Check:
```bash
curl http://localhost:11434/v1/models  # List loaded Ollama models
```

If you're using a remote Ollama instance, set `MNEMO_SUMMARY_URL` to point to it.

**Server unreachable**

If `mnemo-cortex health` can't reach the API, check:
```bash
curl http://localhost:50001/health    # Or your MNEMO_URL
```

Common causes: wrong port, firewall blocking, server not started. On multi-machine setups, ensure the target host's firewall allows the port (e.g., `ufw allow from 10.0.0.0/24 to any port 50001`).

## Verify Installation

After setup, run the smoke test to confirm everything works:

```bash
cd /path/to/mnemo-cortex
source .venv/bin/activate
pytest tests/test_smoke.py -v
```

Expected output (all 4 assertions must pass):

```
tests/test_smoke.py::test_ingest_compact_expand PASSED

What it verifies:
  ✅ Ingest: 24 messages stored successfully
  ✅ Conversation: agent/session pair created
  ✅ Compaction: summaries generated from message chunks
  ✅ Expansion: summary expands back to source messages (verbatim)
```

If the test fails, check that all Python dependencies are installed (`pip install -e .`).

## Architecture

```
mnemo_v2/
  api/server.py              FastAPI app (optional — v2 works without it)
  db/schema.sql              Canonical schema + FTS5 tables
  db/migrations.py           Schema bootstrap and compatibility checks
  store/ingest.py            Durable transcript ingest + tape journaling
  store/compaction.py        Leaf/condensed compaction with LLM summarization
  store/assemble.py          Active frontier → model-visible context
  store/retrieval.py         FTS5 search + source-lineage replay
  watch/session_watcher.py   Tails JSONL session logs into the store
  watch/context_refresher.py Writes MNEMO-CONTEXT.md on an interval
```

### Design Rules

- Immutable transcript in `messages`
- Mutable active frontier in `context_items`
- Summaries are derived, never destructive
- Raw tape is append-only for crash recovery
- Compaction events are journaled
- Replay supports `snippet` or `verbatim`
- Expansion is always scoped to a conversation

### Schema

See [`mnemo_v2/db/schema.sql`](mnemo_v2/db/schema.sql) for the full schema. Key tables:

| Table | Purpose |
|-------|---------|
| `conversations` | Agent + session pairs |
| `messages` | Immutable transcript (role, content, seq) |
| `summaries` | Compacted summaries with depth and lineage |
| `summary_messages` | Links summaries to source messages |
| `summary_sources` | Links condensed summaries to leaf summaries (DAG) |
| `context_items` | The active frontier (what the agent sees) |
| `compaction_events` | Audit log of all compaction operations |
| `raw_tape` | Append-only crash recovery journal |

## Mnemo Cortex vs OpenClaw Active Memory

OpenClaw 2026.4.10 shipped a native Active Memory plugin. Some people have asked whether it replaces Mnemo Cortex. Short answer: no — they solve different problems. Here's the difference, based on testing both on our Sparky sandbox agent.

|                     | Active Memory (native)         | Mnemo Cortex (MCP)                          |
|---------------------|-------------------------------|---------------------------------------------|
| **Scope**           | Single agent                  | Cross-agent (multi-agent bus)               |
| **Store**           | Local workspace files + FTS   | Centralized SQLite + embeddings             |
| **Persistence**     | Per-agent, per-workspace      | Survives resets, sessions, machine moves     |
| **Cross-session**   | Within one agent's workspace  | Any agent, any machine                      |
| **Integration**     | Independent store             | Independent store                           |

### When to use which

- **Active Memory:** Intra-session, same-agent, fast local recall. Your agent's personal scratchpad.
- **Mnemo Cortex:** Cross-agent memory bus. When Agent A needs to know what Agent B learned. When memory must survive session resets, machine moves, or agent restarts.

We run both. Active Memory handles per-agent recent context. Mnemo handles everything that crosses agents or needs durable archival. They stack; they don't compete.

## Origin Story

For two years, Guy Hutchins — a 73-year-old maker in Half Moon Bay — acted as the "Human Sync Port" for his AI agents, manually copying transcripts between sessions. Then came OpenClaw, Rocky, and a $100 Claude subscription. In one session, Guy, Rocky, and Opie designed a memory coprocessor that actually worked. They named it Mnemo Cortex.

v2.0 was a team effort: **Opie** (Claude Opus) designed the architecture, **AL** (ChatGPT) built the implementation, **CC** (Claude Code) deployed and integrated it, **Alice** and **Rocky** (OpenClaw agents) served as live test subjects, and **Guy Hutchins** made it all happen.

Read the full story: [Finding Mnemo](FINDING-MNEMO.md)

## Credits

**The Sparks team:**
- **Guy Hutchins** — Project lead, testing, and the reason any of this exists
- **Rocky Moltman** 🦞 — Creative AI partner, first v2.0 production user
- **Opie** (Claude Opus 4.6 / 4.7) — Architecture design, schema design, compaction strategy
- **AL** (ChatGPT) — Implementation, watcher/refresher daemons, test suite
- **CC** (Claude Code) — Deployment, integration, live testing, bug fixes; built WikAI compiler + Sparks Bus
- **Alice Moltman** — Live test subject on THE VAULT, first v2.0 user

**External inspirations** (the Clapton Method — adopt the best ideas, credit openly, build on top):
- **Andrej Karpathy** — [LLM Wiki pattern](https://gist.github.com/karpathy), April 2026. Inspired WikAI's compile-don't-rederive design and the "idea file as publishing format" pattern used in `SETUP-PROMPT.md`.
- **Nate B Jones** — [OpenBrain](https://github.com/NateBJones-Projects/OB1) + ["Your AI Does the Hard Work Then Deletes It" (YouTube)](https://youtu.be/dxq7WtWxi44) + [Substack](https://natesnewsletter.substack.com/). Inspired our three-layer memory architecture (structured store + compiled wiki + ephemeral brain files).
- **Google A2A Protocol** — [github.com/google/A2A](https://github.com/google/A2A). Sparks Bus speaks A2A data shapes; transport is v2 roadmap.
- **Mem0** — [mem0.ai](https://mem0.ai). The Mem0 Bridge is "and Mem0, not instead of Mem0."
- **[Lossless Claw](https://github.com/Martian-Engineering/lossless-claw)** by Martian Engineering — early exploration of lossless conversation logging that informed the v1 capture pattern.

Built for [Project Sparks](https://projectsparks.ai).

## Works Great With

- **[ClaudePilot OpenClaw](https://github.com/GuyMannDude/claudepilot-openclaw)** — free AI-guided setup guide. Get an OpenClaw agent running with memory in one afternoon.

## License

MIT
