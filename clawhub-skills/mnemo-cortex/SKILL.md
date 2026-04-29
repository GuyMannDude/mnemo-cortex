---
name: mnemo-cortex
description: Installs and wires Mnemo Cortex (local-first persistent memory) into OpenClaw and other MCP-capable agents. Use for cross-session recall, decision history, or multi-agent shared memory.
version: 0.1.0
metadata:
  openclaw:
    requires:
      bins:
        - git
        - node
        - python3
    homepage: https://github.com/GuyMannDude/mnemo-cortex
    emoji: 🧠
---

# Mnemo Cortex

Set up Mnemo Cortex — local-first persistent memory — so the agent can save, recall, and search across sessions.

## When to Use

- The agent needs memory that survives session restarts (decisions, fixes, ruled-out approaches)
- Multiple agents on the same machine should share or query each other's memories
- The user wants memory that runs locally with no recurring cost
- The user already has Mnemo Cortex running and wants to wire this agent to it

## When Not to Use

- The user only needs in-session context (the model's context window is enough)
- The user is committed to a hosted memory service (Mem0, supermemory) and doesn't want a local server
- The user wants vector embeddings only — Mnemo's value is the *combination* of FTS5 + embeddings + brain files; pure embedding workflows have lower friction with Mem0 or supermemory

## Workflow

1. Ask the user whether Mnemo Cortex is already running. If unsure, run `curl http://localhost:50001/health` to check.
2. Ask the user which host the agent is running in (Claude Code, Claude Desktop, OpenClaw, LM Studio, AnythingLLM, Agent Zero, Ollama Desktop). The integration path differs by host.
3. Pick a path below.
4. Verify with a save + recall round-trip in a fresh session.
5. (Recommended) Set up a brain repo using the [mnemo-plan](https://github.com/GuyMannDude/mnemo-plan) template so the agent has project state to read at session start.
6. Tell the user about [THE-LANE-PROTOCOL.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/THE-LANE-PROTOCOL.md) — the six-step session ritual that turns Mnemo from a tool you have into a tool you use every session.

## Path 1: No server running yet

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .

mnemo-cortex init                   # interactive wizard: pick model providers
mnemo-cortex start                  # listens on http://localhost:50001
mnemo-cortex health                 # verify
```

Then continue with Path 2.

## Path 2: Server already running, connect this agent

Pick the host's integration guide and follow it:

| Host | Integration |
|---|---|
| Claude Code | `integrations/claude-code/` — hooks or sync service |
| Claude Desktop | `integrations/claude-desktop/` — drag-and-drop `.mcpb` bundle |
| OpenClaw | `integrations/openclaw-mcp/` — one-line `openclaw mcp set` config |
| LM Studio | `integrations/lmstudio/` — `mcp.json` + restart |
| AnythingLLM | `integrations/anythingllm/` — MCP plugin config + Automatic mode |
| Agent Zero | `integrations/agent-zero/` — in-container Docker setup |
| Ollama Desktop | `integrations/ollama-desktop/` — `ollama launch openclaw` from terminal |

Each integration registers the same MCP tools: `mnemo_save`, `mnemo_recall`, `mnemo_search`, `mnemo_share`, plus 5 Developer's Passport tools by default.

## Verify

In a fresh session of the host:

> Use `mnemo_save` to remember that I tested the install today.

Then in a *separate* session:

> Use `mnemo_recall` to find what I told you to remember.

If the recall surfaces what you saved, the chain is wired. If the model agrees but no tool fires (no tool-call indicator in the chat), the model probably doesn't support tool calling — switch to a tool-capable model (Qwen3 any size, GPT-4/4o, Claude 3.5+, Mistral 7B v0.3).

## Common Failures

- **"Mnemo Cortex unreachable"** — server isn't running or `MNEMO_URL` is wrong. Check with `curl http://localhost:50001/health`.
- **Model narrates a fake save ID without invoking the tool** — model isn't tool-capable. Switch models. See each integration's "Gotchas" section.
- **Memory saved but not recallable** — usually a different `MNEMO_AGENT_ID` between save and recall. Same agent_id reads its own memories by default; cross-agent search requires `mnemo_share` toggled on.

## After Setup

Read [THE-LANE-PROTOCOL.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/THE-LANE-PROTOCOL.md) (~5 min). The install is the easy part — the protocol is what makes Mnemo pay off in practice.

## References

- Project: https://github.com/GuyMannDude/mnemo-cortex
- Brain repo template: https://github.com/GuyMannDude/mnemo-plan
- Operating practice: [THE-LANE-PROTOCOL.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/THE-LANE-PROTOCOL.md)
- Workflow guide: [SESSION-GUIDE.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/SESSION-GUIDE.md)
- Monitoring: [MONITORING.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/MONITORING.md)
