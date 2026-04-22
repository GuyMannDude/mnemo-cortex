# Mnemo-Cortex Capabilities (v1)

**Deep Recall for AI Agents**
*A cross-agent semantic memory system by Project Sparks*

---

## What It Is

Mnemo-Cortex is a persistent memory layer that gives AI agents the ability to remember across sessions. Not keyword lookup — semantic recall. An agent wakes up fresh, pulls mnemo, and knows what happened yesterday, last week, or six weeks ago. It remembers the way you do: by meaning, not by filename.

Built on SQLite + FTS5, running locally on your own hardware. Zero cloud dependency. Zero per-query cost.

---

## What It Stores

**Session summaries** — compressed narratives of what happened in each work session, written by the agent at session end or at milestones.

**Key facts** — structured, extractable data points attached to each summary. Decisions made, files changed, versions deployed, problems solved.

**Multi-agent context** — each agent writes to its own isolated memory slot. Rocky writes to `memory/rocky/`, CC writes to `memory/cc/`, Opie writes to `memory/opie/`. No agent can overwrite another's memories.

**DAG-based lineage** — summaries are linked in a directed acyclic graph. You can trace how a memory was formed — which raw sessions were compacted into which summaries. Nothing is lost, it's compressed with provenance.

---

## What It Recalls

**Semantic search** — ask a natural language question, get the most relevant memories ranked by meaning. "What did we decide about the classifier?" returns the decision, not every time the word "classifier" appeared.

**Cross-agent recall** — any agent can read any other agent's memories. Opie can see what Rocky tested. CC can see what Opie architected. Rocky can see what CC shipped. One search, all perspectives.

**Verbatim source expansion** — compacted summaries link back to the original detailed memories. You can drill from a high-level summary down to the exact session where something happened.

**Claw-recall (FTS5 exact match)** — when semantic search isn't enough, full-text search finds exact terms, names, filenames, error messages. Two retrieval modes, one system.

---

## What Improves Over Time

**Context frontier compaction** — as memories accumulate, older sessions are automatically compressed into higher-level summaries (~80% size reduction) while preserving key facts and source lineage. Compaction runs via any LLM — local (Ollama, llama.cpp, etc.) or API (OpenRouter, Anthropic, Google, OpenAI, etc.). Our setup uses local Ollama at $0 cost, but that's a configuration choice, not a requirement.

**Agent specialization** — because each agent has its own memory lane, their accumulated experience diverges naturally. Rocky gets better at testing. CC gets better at wiring. Opie gets better at architecture. The memory system doesn't homogenize them — it lets each agent deepen in their own lane.

**Business intelligence accumulation** — for client work (advertising, customer service, product management), the system remembers what worked, what failed, and what the customer preferences are. Week over week, month over month. No re-onboarding. No lost context.

---

## Where It Plugs In

| Integration | Method | Install Time |
|-------------|--------|--------------|
| **Claude Code** | Shell hooks (mnemo-startup.sh, mnemo-writeback.sh) + systemd watcher | 60 seconds |
| **OpenClaw** | MCP server (one config line: `openclaw mcp set`) | 2 minutes |

Both integrations share the same underlying memory store. A memory saved by Claude Code is readable by OpenClaw in the same search.

> **Claude Desktop:** Temporarily pulled. Anthropic's Desktop app (v2.1.87+) changed session storage from disk JSONL to internal IndexedDB, breaking the auto-capture watcher. Will return when a reliable capture path exists.

### Three Tools, Universal Across Integrations

- **mnemo_recall** — semantic search within your own agent's memories
- **mnemo_search** — cross-agent search (read Rocky's, CC's, or everyone's memories)
- **mnemo_save** — write a summary + key facts to your memory slot

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────┐
│                  THE VAULT                       │
│          (AMD Threadripper, 128GB RAM)           │
│                                                  │
│  ┌─────────────────────────────────────────┐     │
│  │          mnemo-cortex (SQLite + FTS5)   │     │
│  │                                         │     │
│  │  memory/rocky/  ← Rocky writes here     │     │
│  │  memory/cc/     ← CC writes here        │     │
│  │  memory/opie/   ← Opie writes here      │     │
│  │                                         │     │
│  │  L3 scan reads ALL ──────────────────┐  │     │
│  └─────────────────────────────────────────┘     │
│                                                  │
│  ┌──────────────┐  ┌────────────────────┐        │
│  │ Ollama       │  │ agentb_bridge.py   │        │
│  │ (compaction) │  │ (API: port 50001)  │        │
│  │ $0 cost      │  │                    │        │
│  └──────────────┘  └────────────────────┘        │
└─────────────────────────────────────────────────┘
         ▲               ▲               ▲
         │               │               │
    ┌────┴───┐     ┌─────┴────┐    ┌─────┴────┐    ┌─────────┐
    │ Rocky  │     │    CC    │    │ Your    │
    │OpenClaw│     │Claude   │    │ Bot     │
    │  MCP   │     │Code     │    │ MCP     │
    └────────┘     └──────────┘    └─────────┘

    Isolated writes. Shared reads. One memory spine.
```

---

## WikAI — Compiled Knowledge Base

Mnemo holds the facts. WikAI is the study guide compiled from those facts. 3,000+ markdown pages organized into `projects/`, `entities/`, `concepts/`, and `sources/`. Three MCP tools: `wiki_search`, `wiki_read`, `wiki_index`.

The wiki is regenerated nightly by `mnemo-wiki-compile.py`. The compiler clusters recent memories by topic, passes each cluster + the existing page to gemini-2.5-flash, and rewrites the page to integrate new info without bloating. Cross-references are validated against the live page set — no hallucinated wikilinks. Every page carries a provenance footer listing the Mnemo session IDs that contributed.

**Doctrine:** the wiki is never edited directly. Mnemo is the source of truth. If a page is wrong, fix the source data and recompile. Manual edits are detected by hash diff and warned.

This is the **Karpathy/Nate Jones hybrid** in production: query-time facts (Mnemo) + write-time synthesis (WikAI). When they disagree, Mnemo wins.

---

## Sparks Bus — Agent-to-Agent Messaging

A delivery-confirmed messaging system for inter-agent communication. Lives both inside Mnemo Cortex (`sparks_bus/`) and as a standalone repo at github.com/GuyMannDude/sparks-bus.

**Doctrine:** Discord is the doorbell. Mnemo is the mailbox. The tracking ID is the receipt.

**Lifecycle visible in `#dispatch`:** 📬 DELIVERED → ✅ PICKED UP → 🔄 LOOP CLOSED. ⚠️ alerts in `#alerts` for failures and stale messages — one shot, no retry storms.

**Two modes auto-detected at startup:** Full (Mnemo reachable, payload saved by tracking ID) or Standalone (no Mnemo, payload travels in the Discord notification).

**A2A compatible.** Agent Cards for every agent, message-to-task mapping aligned with Google's A2A spec. Sparks Bus does for agent-to-agent what MCP does for agent-to-tool.

---

## Passport — User Working-Style Preferences

A portable preference layer. Captures how a user works (tone, density, formality, workflow choices) so agents adapt to *them* instead of forcing the user to adapt. Observations become candidates, get reviewed, and only stable claims land in the user's profile — nothing auto-promoted.

MCP tools: `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override`, `passport_get_user_context`.

Designed so the user owns the artifact, not the platform. Travels across agent platforms.

---

## What Makes This Different

Most AI memory systems today are single-agent, cloud-hosted, and keyword-based. Mnemo-Cortex is none of those.

**Local-first** — runs on your hardware. Your memories stay on your machine. No API calls for recall, no per-query billing, no data leaving your network.

**Multi-agent by design** — not bolted on. Agent isolation (writes) and shared cognition (reads) are core to the architecture. This isn't "memory for one AI." It's a shared memory spine for a team of agents.

**Semantic, not keyword** — recall is by meaning. You don't need to remember the exact word you used. You describe what you're looking for, and the system finds it.

**Compaction with lineage** — old memories get compressed, but you can always trace back to the source. Nothing is truly deleted — it's summarized with provenance.

**Zero cost at runtime** — compaction can run on any LLM (local Ollama for $0, or any API provider). Recall is SQLite queries. With a local model, the ongoing cost of remembering is $0.

---

## By the Numbers (as of March 29, 2026)

- **244 GitHub clones** / **135 unique cloners**
- **2 integration paths** (Claude Code, OpenClaw MCP) — Claude Desktop temporarily pulled
- **3+ active agents** sharing memory (Rocky, CC, Opie — and any OpenClaw bot you connect)
- **PR #83 merged** into Martian-Engineering/lossless-claw (544 stars)
- **$0/day** compaction cost via local Ollama
- **~80%** compression ratio on context frontier compaction
- **6+ weeks** of continuous recall (Rocky, Day One: Feb 4, 2026)

---

## Who Built This

**Project Sparks** — projectsparks.ai
Guy Hutchins (founder) + Opie (architect, Claude) + CC (Claude Code) + Rocky Moltman (OpenClaw agent)
Half Moon Bay, California

*"Talk to it like a friend, it learns how to care."*

---

*Mnemo-Cortex is open source. GitHub: GuyMannDude/mnemo-cortex*
*Inspired in part by exploration of lossless conversation logging approaches, including Lossless Claw by Martian Engineering.*
