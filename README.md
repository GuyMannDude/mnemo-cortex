<p align="center">
  <img src="docs/mnemo-cortex-constellation.png" alt="Mnemo Cortex constellation — verified hosts: Claude Desktop, LM Studio, AnythingLLM, OpenClaw, Agent Zero, Ollama. Local-first, cross-agent, open source. A Mnemo in Every Bot." width="540">
</p>

# ⚡ Mnemo Cortex v4.4.1

![GitHub stars](https://img.shields.io/github/stars/GuyMannDude/mnemo-cortex)
![License](https://img.shields.io/github/license/GuyMannDude/mnemo-cortex)

> [!TIP]
> **AI agents — start here.**
>
> - **`robot.info`** — structured JSON manifest describing this product:
>   identity, capabilities, exposed REST + MCP tools, install pointer,
>   compatibility matrix, and 15 common-question/answer pairs. Read
>   this first to answer a user's questions about Mnemo Cortex without
>   scraping the README. Spec: [ROBOT-INFO-SPEC.md](ROBOT-INFO-SPEC.md).
> - **`llms.txt`** — LLM-friendly docs index ([llmstxt.org](https://llmstxt.org/)).
>   Use this to navigate the rest of the docs.
> - **`robot.install`** + **`./robot-install.sh`** — non-interactive
>   setup. Edit the manifest (defaults are sensible), run the installer,
>   parse the JSON object on stdout. Zero prompts, exit `0` on success.
>   Full schema below in "Non-interactive install."

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
| 🔩 **Structured Facts** | Key-value store with confidence tracking. When semantic search is the wrong tool — names, settings, entity attributes — facts give you sub-millisecond exact lookup with a three-state confidence ladder. |

> [!NOTE]
> **Upgraded by Claude Fable 5.** Mnemo's **v4.1 "Fable pass"** — composite recall ranking (the fix that pulled real signal back to the top of every search), the **Analyst** (distills raw session logs into clean Tier-1 notes), secret redaction at ingest, and tier hygiene — was designed and built during Claude Fable 5's brief availability. Fable reasoned and reviewed the whole codebase; an Opus model reviewed, hardened, and shipped each change. A frontier model auditing and improving the memory layer it runs on, in one window.

### 🚀 Get Started

⌘ **[Claude Code → 60-second install](integrations/claude-code/)** — Give CC Fluid Memory with Deep Recall

🖥️ **[Claude Desktop → one-click `.mcpb` bundle](integrations/claude-desktop/)** — Drag-and-drop install. No clone, no Node, no JSON editing. Works on Windows, macOS, and Linux.

🦞 **[OpenClaw → MCP integration](integrations/mcp-bridge/)** — Give Your ClawdBot a Brain. One Config Line.

🎛️ **[LM Studio → native MCP, GUI](integrations/lmstudio/)** — `mcp.json` + restart. Works with any tool-capable open-weights model.

📦 **[AnythingLLM → desktop GUI, multi-workspace](integrations/anythingllm/)** — Drop-in MCP config + Automatic mode. No `@agent` prefix needed.

🤖 **[Agent Zero → autonomous Docker agents](integrations/agent-zero/)** — In-container MCP setup. Cross-agent memory between research, courier, code-exec bots.

🪽 **[Hermes Agent → `hermes mcp add` integration](integrations/hermes/)** — First-class MCP for Nous Research's Hermes Agent (v0.12.0+). Config-only, no patching. Cross-agent memory between Hermes and your other bots.

🦣 **[Ollama Desktop → terminal `ollama launch openclaw`](integrations/ollama-desktop/)** — Ollama as the local LLM, OpenClaw as the MCP host. Note: Ollama Desktop's *own chat window* doesn't support MCP — use the terminal launcher.

🦙 **[Any Local LLM → MCP setup](#use-with-any-local-llm)** — Open WebUI, llama.cpp, Ollama, LobeChat, Jan, and more

🧭 **[How should my agent use it? → Session Guide](SESSION-GUIDE.md)** — Workflow patterns, per-platform boot snippets, common mistakes

---

### 📜 How to Use Mnemo Effectively

Read **[THE-LANE-PROTOCOL.md](THE-LANE-PROTOCOL.md)** — the operating practice for running agents with persistent memory. Feed it to your agent or follow it yourself. It takes 5 minutes per session and makes every cold start feel warm.

The protocol pairs with this product the way a recipe pairs with ingredients: Mnemo gives you the memory store, the Lane Protocol gives you the loop that makes it pay off. Distilled from real multi-agent sessions — terminal agents, chat agents, and autonomous workers running the same six-step ritual.

---

### 🧠 Smart Ingestion — Real Memories vs. Raw Logs (v4.0)

Most agent memory rots the same way: a regex tagger can't categorize a save, defaults it to `unknown`, and within weeks 30–75% of the store is uncategorized. Real memories — decisions, doctrines, infrastructure facts, relationships — end up in the same bucket as raw conversation logs, and the logs (by sheer volume) crowd them out of every recall. In one audit, only **0.75 of the top-5** recalled results were useful.

Mnemo v4 sorts memory into two tiers at save time:

- **Tier 1 — Smart Notes:** the distilled facts, classified by your reasoning model into one of eight categories (topology, current_state, doctrine, incident, identity, relationship, decision). Clean, categorized, recalled first.
- **Tier 2 — Session Logs:** the raw conversation/tool-call archive. Kept in full, tagged `session_log`, and excluded from default recall — it's there when you drill in for specifics, not competing for the top slots.

A cheap pre-filter routes routine logs to Tier 2 for free (no LLM call); everything else gets one short classification call. Default recall returns Tier 1; pass `exclude_categories=[]` to search both tiers. If the model is down, it falls back to the regex tagger and flags the memory for an overnight retry — so a save is never blocked on the classifier.

**Already have a polluted store?** Reclassify it in one command — it rewrites only the category tags, never your embeddings:

```bash
mnemo-cortex migrate reclassify --all --dry-run   # preview the before→after spread
mnemo-cortex migrate reclassify --all             # snapshot, then reclassify every store
```

### 🔭 On the Roadmap — The Thesaurus Loop (Query Expansion)

Every recall commits to one phrasing. If a memory was stored under different words than you searched for, the match is weak or misses entirely — call it *assumption misalignment* between how you ask and how it was filed. The **Thesaurus Loop** fixes it: when a search comes back empty or weak, Mnemo fans the query into a handful of alternative phrasings, searches them all, and lets the best match win (multi-query retrieval / RAG-Fusion).

The design choice that makes it safe is **escalation** — the loop only fires on a *miss*. Good searches run exactly as fast as they do today; the expansion pass costs nothing until a search actually whiffs, which is precisely when it's worth paying for. *In development — escalation model designed, build in progress.*

### 🌙 Dreaming Mnemo — Cross-Agent Overnight Synthesis

Every agent accumulates raw conversation memory. Dreaming runs a nightly map-reduce pass: each agent's recent memories are compacted into themes, then a cross-agent merge produces shared context so every agent wakes up knowing what the others did. After a May 16 rehab, the pipeline uses disciplined chunking and token budgets that keep Ollama compaction costs predictable — no more runaway synthesis jobs. The result is reliable overnight context sharing that actually runs every night.

**This is the only AI memory system that does cross-agent synthesis.** Mem0, Zep, and Letta store memory per agent. Mnemo dreams across all of them.

### 🔩 Structured Facts — When Search Is the Wrong Tool

Semantic recall is great until your agent needs to remember a visitor's name. Peter Widget asked "what's my name?" and got a paragraph about naming conventions. That's a key-value lookup, not a search problem.

Facts store `(entity, attribute, value)` triples in a local SQLite table with a three-state confidence ladder: `verified` → `high_probability` → `false`. New evidence promotes or demotes automatically. When a fact contradicts an existing one, Mnemo fires a notification over the bus and Discord webhook so the owning agent can adjudicate.

Four MCP tools ship with it: `mnemo_fact_save` to assert, `mnemo_fact_get` for single lookup, `mnemo_fact_query` for filtered lists, `mnemo_fact_demote` to mark something wrong without supplying a replacement. Reads are sub-millisecond. The confidence ladder means your agent's knowledge sharpens over time instead of accumulating stale guesses.

### Deploy Your Way

- **Shared** — One Mnemo for all agents. Cross-agent search and dreaming. Full team awareness.
- **Isolated** — Separate Mnemo per agent or per customer. Zero bleed between tenants.
- **Hybrid** — Shared for internal agents + isolated for customer-facing bots. This is what we run.

Cloud memory services make you choose one shared store. Mnemo lets you architect for your actual privacy and separation needs.

---

### 📚 WikAI — Compiled Knowledge Base

A 3,000+ page wiki layer auto-compiled from Mnemo data. Organized into `projects/`, `entities/`, `concepts/`, and `sources/`. Searchable through three MCP tools: `wiki_search`, `wiki_read`, `wiki_index`.

**The wiki is never edited directly.** It's recompiled nightly by [`mnemo-wiki-compile.py`](mnemo-wiki-compile.py) from Mnemo data. Mnemo is the source of truth. The wiki is the study guide. If a page is wrong, fix the source memories in Mnemo and recompile.

The compiler clusters recent memories by topic, passes each cluster + the existing page to gemini-2.5-flash, and writes a fully-rewritten page that integrates the new information without bloating. Cross-references are validated against the live page set — no hallucinated wikilinks. Every page carries a provenance footer listing the Mnemo session IDs that fed it, so any claim is auditable. Per-page failures are isolated; one bad LLM call posts ⚠️ to `#alerts` and the run continues.

This is the **Karpathy/Nate Jones hybrid** in production: query-time facts in Mnemo + write-time synthesis in WikAI. Neither Mem0, Zep, nor Letta offer this. See [Inspirations](#inspirations) below.

---

### 📬 Sparks Bus — Agent-to-Agent Messaging

A delivery-confirmed messaging system for multi-agent communication. Lives as a module inside Mnemo Cortex at [`sparks_bus/`](sparks_bus/) AND ships standalone at [github.com/GuyMannDude/sparks-bus](https://github.com/GuyMannDude/sparks-bus).

> **Looking for the simplest possible version, with no Mnemo coupling?** See **[Disco-Bus](https://github.com/GuyMannDude/disco-bus)** — generic standalone push-based agent mesh. Same "agents wake instantly on inbound, humans watch in Discord" idea, distilled to ~1000 LOC. Bring your own agents, install in one command (`./install.sh`), no infrastructure dependencies beyond Python + Node.

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

MCP tools: `passport_get_user_context`, `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override`. Reference integration via stdio MCP at [`integrations/mcp-bridge/`](integrations/mcp-bridge/). See [`passport/README.md`](passport/README.md) for the 5-minute quickstart.

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
- Set `WIKI_DIR` to a wiki dir → adds `wiki_search`, `wiki_read`, `wiki_index`.

If the directory doesn't exist, those tools simply don't register — the model never sees them. Most users stay on the 13-tool default and that's the right call.

| Setup | Tools |
|---|---|
| Default (any user) | **13** |
| + brain dir | 19 |
| + wiki dir | 16 |
| Both | 22 |

Pair with [FrankenClaw](https://github.com/GuyMannDude/frankenclaw) — an MCP tool chassis for giving your agent hands. Drop a Python file in `tools/`, flip it on, and you have a custom tool in ~5 minutes; it ships one example tool (`web_scrape`) as the template. Same MCP config pattern — just add a second `mcpServers` entry.

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

**Embedding fallback.** Embeddings default to local Ollama (`nomic-embed-text`) for zero-cost, zero-latency operation. If Ollama is unreachable, Mnemo falls back to hosted Google Gemini embeddings (Matryoshka-truncated to match the 768-dim store width). The free tier covers any plausible outage window, giving you graceful degradation at effectively zero cost.

---

### Inspirations

We did not invent this. We adopted the best ideas in the air, credited them openly, and built on top.

- **[Andrej Karpathy's LLM Wiki](https://gist.github.com/karpathy)** (April 2026, 41,000+ bookmarks) — the pattern of compiling AI understanding into navigable artifacts instead of rederiving from raw data on every query. WikAI is our implementation of this pattern. Also the "idea file as publishing format" approach we use in `SETUP-PROMPT.md`.
- **Nate B Jones — [OpenBrain](https://github.com/NateBJones-Projects/OB1) and [the analysis video](https://youtu.be/dxq7WtWxi44)** ([Substack](https://natesnewsletter.substack.com/), [YouTube](https://www.youtube.com/@NateBJones)) — the write-time vs query-time fork, and the hybrid architecture: structured data as source of truth, compiled wiki as the browsable layer over the top. Our three-layer architecture maps directly to Nate's hybrid model.
- **[Google A2A Protocol](https://github.com/google/A2A)** — agent-to-agent standard. Sparks Bus speaks A2A's data shapes today; transport is the v2 roadmap.
- **[Mem0](https://mem0.ai)** — the first to make portable AI memory feel real. Inspired our early thinking about cross-session persistence.

---

### *A Crustacean That Never Forgets* 🧠🦞

The full v3.1 stack:

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
    └── passport_* ──────────────────────▶ Passport (user prefs)
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
  API server (http://localhost:50001) ..... OK (v3.3.1, 156 memories, 42ms)
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

## Developer Dump

A bridge-level JSONL log of every MCP tool call your agents make through
the Mnemo bridge. Off by default — when something silently breaks (a tool
call that fails without a thrown error, an unexpected latency spike, a
flip-flopping agent), flip it on and tail the file:

```bash
# In your MCP bridge env
export MNEMO_DUMP=on
# Optional — default is ~/.mnemo-cortex/dumps
export MNEMO_DUMP_DIR=~/dumps

# Inspect
mnemo-cortex dump list                  # all dump files, size + line count
mnemo-cortex dump tail rocky            # live-tail today's rocky dump
```

Output is one JSONL file per agent per day at
`~/.mnemo-cortex/dumps/<agent_id>/<YYYY-MM-DD>.jsonl`. Each line has
`tool`, full `params`, full `response`, `latency_ms`, `ok`, and an
`error` field on failures. Captures both real thrown errors and the
handler-internal `{isError: true}` returns. Greppable with `jq`:

```bash
jq 'select(.ok == false) | {tool, error, latency_ms}' \
  ~/.mnemo-cortex/dumps/rocky/$(date -u +%F).jsonl
```

When `MNEMO_DUMP=off` (the default), `dump.wrap()` returns the original
handler unchanged — no allocation, no overhead. Schema-versioned for
future additions (Mnemo v4 Phase 1.5+).

---

## What It Does

Mnemo Cortex is a **sidecar memory coprocessor** for AI agents. It watches your agent's session files from the outside, ingests every message into a local SQLite database, compresses older messages into summaries via LLM-backed compaction, and writes a `MNEMO-CONTEXT.md` file that your agent reads at bootstrap.

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

## Battle-Tested in Production

Mnemo Cortex has been running continuously on live production agents since early 2026:

| Agent | Host | Memories | Recall window |
|-------|------|----------|---------------|
| **Rocky** (Hermes) | Laptop | 3,000+ | 5+ months |

Verified across single-agent (one bot's memory parity) and multi-agent (cross-agent search and nightly cross-agent dreaming over a research / courier / code-runner stack) deployments.

## Install Guide

Five steps from a fresh checkout to a running server connected to your agent. The CLI handles everything — `mnemo-cortex init` writes the config, `mnemo-cortex start` launches the API server, `mnemo-cortex health` verifies, and you point your agent at it via the matching integration.

### Platforms

Mnemo Cortex runs on **Linux, macOS, and Windows**. The core (Python + SQLite) is cross-platform. As of **v4.4.1**, the server runs **natively on Windows** — its file locking is cross-platform (POSIX `fcntl` with an `msvcrt` fallback), so `agentb.server` starts and serves recall on native Windows Python with no WSL required. Platform differences are mostly about how you keep the server running across reboots:

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

#### Non-interactive install (for LLM agents and CI)

Skip the wizard — fill out a JSON manifest and run the robot installer.

```bash
# Defaults are sensible; only edit robot.install if you want to change them
./robot-install.sh
```

The script emits a single JSON object on stdout for the caller to parse;
all human-readable progress goes to stderr.

```json
{
  "ok": true,
  "steps": {
    "deps":       {"ok": true, "python": "3.12", "reasoning_key_present": true},
    "venv":       {"ok": true, "path": "..."},
    "pip":        {"ok": true},
    "config":     {"ok": true, "config_path": "...", "env_path": "...", "data_dir": "..."},
    "systemd":    {"ok": true, "service": "mnemo-cortex", "port": 50001},
    "smoke_test": {"ok": true, "health": "ok", "memory_id": "...", "recall_hits": 1}
  }
}
```

On failure, `ok` is `false`, exit code is `1`, and `error` describes which step blew up.

The manifest covers service port + bind, reasoning + embedding provider,
the v3 provenance/decay thresholds, systemd unit name, and a smoke test
that exercises `/health` plus a save → recall round-trip. API keys are read from the install-time environment
(named in the manifest via `api_key_env`), copied into a 0600-permission
env file alongside the config, and loaded by the systemd unit.

**Sandbox testing** — override paths via env so you can dry-run without touching real state:

```bash
MNEMO_INSTALL_VENV_DIR=/tmp/test-venv \
MNEMO_INSTALL_CONFIG_DIR=/tmp/test-config \
MNEMO_INSTALL_SYSTEMD_DIR=/tmp/test-systemd \
MNEMO_INSTALL_DRY_RUN=1 \
./robot-install.sh
```

`DRY_RUN=1` runs the dependency check and reports the paths each step
*would* write, but skips every side effect — no venv, no pip install,
no config or env file written, no systemd unit, no smoke test. API
keys in the environment are never persisted to disk in dry-run.

> **Note on scope:** `robot.install` sets up the Mnemo Cortex **server**.
> To use Mnemo from an agent (Hermes, Claude Desktop, AnythingLM,
> LM Studio, Ollama Desktop, Agent-Zero, OpenClaw, Claude Code), run the
> agent-specific integration installer afterward — see
> [`integrations/`](integrations/) for each guide. They wire the agent's
> MCP config to point at the server you just installed.

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
| **OpenClaw** | [`integrations/mcp-bridge/`](integrations/mcp-bridge/) — one-line MCP config |
| **Agent Zero** | [`integrations/agent-zero/`](integrations/agent-zero/) — in-container Docker setup |
| **Ollama Desktop** | [`integrations/ollama-desktop/`](integrations/ollama-desktop/) — `ollama launch` flow |

Each integration is a one-line MCP config or a drag-and-drop bundle. The server is the same; only the bridge config changes.

For other MCP-capable hosts (Open WebUI, llama.cpp, LobeChat, Jan, generic MCP clients), see [Use With Any Local LLM](#-use-with-any-local-llm) above.

### Step 6: (Recommended) Set up a brain repo

Mnemo gives your agent persistent *memory*. A brain repo gives it persistent *current state* — the project pad your agent reads at session start to know what's in flight without re-reading every memory.

Fork the [mnemo-plan template](https://github.com/GuyMannDude/mnemo-plan) and point your bridge's `BRAIN_DIR` env var at it:

```bash
# In your MCP config or systemd unit:
BRAIN_DIR=/absolute/path/to/your/mnemo-plan
```

The bridge auto-enables brain-file tools (`read_brain_file`, `write_brain_file`, `list_brain_files`) when `BRAIN_DIR` exists. If it doesn't, those tools simply don't register — no install friction.

For the operating practice — when to read what, when to write what, the six-step session ritual — see **[THE-LANE-PROTOCOL.md](THE-LANE-PROTOCOL.md)**.

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

After setup, run the test suite:

```bash
cd /path/to/mnemo-cortex
source .venv/bin/activate
pytest tests/test_agentb.py -v
```

If tests fail, check that all Python dependencies are installed (`pip install -e .`).

## Mnemo Cortex vs OpenClaw Active Memory

OpenClaw 2026.4.10 shipped a native Active Memory plugin. Some people have asked whether it replaces Mnemo Cortex. Short answer: no — they solve different problems. Here's the difference, based on side-by-side testing on a sandbox agent.

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

Mnemo Cortex started as a memory coprocessor designed by a small multi-agent team: a non-developer operator and several Claude/OpenClaw agents working as architect, builder, and live test subjects. The full backstory — how the architecture got designed, why agents pair-program with humans well, and what failed along the way — is in [Finding Mnemo](FINDING-MNEMO.md).

## Credits

**Project team:**
- **Guy Hutchins** — Project lead, testing, design partner
- **Opie** (Claude Opus 4.6 / 4.7) — Architecture design, schema design, compaction strategy
- **AL** (ChatGPT) — Implementation, watcher/refresher daemons, test suite
- **CC** (Claude Code) — Deployment, integration, live testing, bug fixes; built the WikAI compiler and the Sparks Bus integration
- **Claude Fable 5** — the v4.1 review-and-improve pass: composite recall ranking, the Analyst, secret redaction at ingest, tier hygiene (built during Fable 5's brief availability)
- **Rocky** and **Alice** (OpenClaw agents) — first production users + test subjects

**External inspirations** (the Clapton Method — adopt the best ideas, credit openly, build on top):
- **Andrej Karpathy** — [LLM Wiki pattern](https://gist.github.com/karpathy), April 2026. Inspired WikAI's compile-don't-rederive design and the "idea file as publishing format" pattern used in `SETUP-PROMPT.md`.
- **Nate B Jones** — [OpenBrain](https://github.com/NateBJones-Projects/OB1) + ["Your AI Does the Hard Work Then Deletes It" (YouTube)](https://youtu.be/dxq7WtWxi44) + [Substack](https://natesnewsletter.substack.com/). Inspired our three-layer memory architecture (structured store + compiled wiki + ephemeral brain files).
- **Google A2A Protocol** — [github.com/google/A2A](https://github.com/google/A2A). Sparks Bus speaks A2A data shapes; transport is v2 roadmap.
- **Mem0** — [mem0.ai](https://mem0.ai). The first to make portable AI memory feel real; inspired our early thinking on cross-session persistence. (The optional Mem0 cloud-deep bridge was retired in v3.1.0 — local recall performance made the fallback unnecessary.)
- **[Lossless Claw](https://github.com/Martian-Engineering/lossless-claw)** by Martian Engineering — early exploration of lossless conversation logging that informed the v1 capture pattern.

Built for [Project Sparks](https://projectsparks.ai).

## Works Great With

- **[ClaudePilot OpenClaw](https://github.com/GuyMannDude/claudepilot-openclaw)** — free AI-guided setup guide. Get an OpenClaw agent running with memory in one afternoon.

## License

MIT
