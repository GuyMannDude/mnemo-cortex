# Mnemo Cortex — Session Guide

> You write it, agents read it. They remember, you don't repeat yourself.

> 📜 **For the full operating practice behind these instructions, see [THE-LANE-PROTOCOL.md](THE-LANE-PROTOCOL.md).** This guide tells the model what to do; the Lane Protocol tells the human (and the model) how to operate the loop.

This guide covers how to use Mnemo Cortex with any LLM agent. Install 
instructions are in the main README. This is about **workflow** — how 
your agent should think about memory, and what to paste into your 
platform's boot file so it actually does it.

---

## How Memory Works (The 60-Second Version)

Mnemo Cortex gives your agent two things:

**Memory** — an event log of what happened. Your agent saves moments 
(decisions, fixes, discoveries) and retrieves them by meaning later. 
Memories accumulate across sessions. They're searchable by any agent 
you authorize.

**Brain file** — a markdown file of what's true *right now*. Current 
project status, priorities, open threads. Your agent reads it at 
session start and rewrites it at session end. Old info gets replaced, 
not appended.

Both matter. Memory without a brain file means your agent remembers 
events but doesn't know what's current. A brain file without memory 
means your agent knows today's state but can't recall how it got there.

### When to use what

| | Persists across sessions | Just this session |
|---|---|---|
| **What's true now** | Brain file | Working memory (context window) |
| **What happened** | `mnemo_save` | Auto-capture / conversation flow |

- Recording a decision or a shipped deliverable → **mnemo_save**
- Updating project status or priorities → **brain file write**
- Routine conversation that doesn't need to survive → let the context window handle it

---

## The Session Lifecycle

Three phases. Works for any agent on any LLM.

### Start — before doing work

1. **Read your brain file.** Know who you are, what's current, what 
   the priorities are.

2. **Recall from Mnemo.** Check what's already been decided or done. 
   Prevents re-work and catches updates from other agents.

### During — while working

3. **Save at decision points.** When something meaningful happens — a 
   decision, a diagnosis, a fix, a shipped feature — save it 
   immediately. Not batched. Not deferred to session end. Include 
   *why*, not just *what*.

What's worth saving:
- "We chose X because Y" — decisions with reasoning
- "X broke because Y, fixed by Z" — diagnoses
- "Shipped X to Y" — deliverables
- "Ruled out X because Y" — prevents re-investigation

What's NOT worth saving:
- "Asked about X" — questions without conclusions
- "Working on X" — status updates without outcomes
- "Read file X" — actions without significance

### End — before closing

4. **Save a session summary.** What was accomplished, what's decided, 
   what's left open. This is what future sessions (yours or other 
   agents') will find when they recall.

5. **Update your brain file.** Rewrite what changed. The next session 
   should open this file and know exactly what's current.

6. **Verify.** Recall what you just saved. Quick sanity check that 
   your memories landed.

---

## Platform Setup

Every LLM platform has a boot file — a markdown document that the 
agent reads automatically at session start. The file name differs by 
platform, but the concept is the same: persistent instructions that 
survive across sessions.

To integrate Mnemo, you add a memory section to your platform's boot 
file. Below are copy-paste blocks for each platform. Pick yours, add 
it to the right file, and your agent starts using Mnemo automatically.

### Claude Code

**Boot file:** `CLAUDE.md` in your project root  
**Global boot file:** `~/.claude/CLAUDE.md` (applies to all projects)

Add this to your `CLAUDE.md`:

```markdown
## Memory — Mnemo Cortex

You have persistent memory via Mnemo Cortex MCP tools.

Session start:
- Read your brain file with read_brain_file
- Call mnemo_recall on your current task before starting work

During work:
- Call mnemo_save when you make a decision, ship a feature, fix a 
  bug, or rule out an approach. Save immediately, not at session end.
  Include reasoning, not just outcomes.

Session end:
- mnemo_save a final summary (what shipped, what's open, what's next)
- Update your brain file with write_brain_file
- Verify with mnemo_recall on your summary

Your local memory (~/.claude/projects/) is auto-loaded by Claude Code 
into your next session's context — fast and free, but private to you. 
Anything that matters beyond this session or across agents goes into 
Mnemo.
```

**Optional auto-capture safety net.** Mnemo Cortex ships an optional 
session-sync service for Claude Code that POSTs your activity to Mnemo 
every 60 seconds without you having to invoke MCP each turn. See 
`integrations/claude-code/README.md` → "Automatic Mode (Session Sync)" 
in the main repo. It runs as a systemd service with an optional 
watchdog for monitoring, and works alongside the explicit `mnemo_save` 
calls above. Manual saves are still where the high-signal memories 
come from — auto-capture is the floor, not the ceiling.

### OpenClaw

**Boot file:** `AGENTS.md` in `~/.openclaw/workspace/`  
**Also loaded:** `SOUL.md`, `USER.md`, `IDENTITY.md`, `TOOLS.md`  
**Optional:** `MEMORY.md` (loaded for normal sessions, not subagents)

Add this to your `AGENTS.md`:

```markdown
## Memory — Mnemo Cortex

You have persistent cross-session memory via Mnemo Cortex MCP tools.

Every session:
- Call mnemo_recall on your current task before doing work
- Read your brain file for current project state

During work:
- Call mnemo_save immediately at decision points. Include what you 
  decided and why. Do not batch saves or defer to session end.

Session end:
- mnemo_save a summary: what you accomplished, decisions made, 
  open threads
- Update your brain file with current state
- Verify by recalling your summary

MEMORY.md is your local workspace memory. Mnemo Cortex is your 
cross-session, cross-agent memory. Use both:
- MEMORY.md for workspace-specific notes and daily journals
- Mnemo for decisions, outcomes, and anything other agents need to see
```

### Cursor

**Boot file:** `.cursorrules` in your project root

Add this to your `.cursorrules`:

```
# Memory — Mnemo Cortex

This project uses Mnemo Cortex for persistent memory via MCP.

When starting work on a task:
- Call mnemo_recall with the task topic to check prior decisions
- Read the brain file for current project state

When you make a meaningful decision or complete something:
- Call mnemo_save immediately with a summary and key_facts
- Include your reasoning, not just the outcome

When finishing a session or switching tasks:
- Save a summary of what was done and what's still open
- Update the brain file with current status
```

### Windsurf

**Boot file:** `.windsurfrules` in your project root

Add this to your `.windsurfrules`:

```
# Memory — Mnemo Cortex

This project uses Mnemo Cortex for persistent memory via MCP.

Before starting any task, call mnemo_recall to check what's already 
been decided or attempted. This prevents duplicate work.

Save to Mnemo (mnemo_save) when you:
- Make a decision and want to record why
- Complete a feature, fix, or deployment
- Rule out an approach (so it's not re-tried)
- End a session (summary of what shipped and what's open)

Read and update the brain file at session start and end to 
maintain current project state.
```

### Codex (OpenAI)

**Boot file:** `AGENTS.md` or `codex.md` in your project root

Add this to your agents file:

```markdown
## Memory — Mnemo Cortex

You have persistent memory via Mnemo Cortex (REST API or MCP).

Before starting work:
- Query Mnemo for prior decisions on your current task
- Read the brain file for current project state

During work:
- Save to Mnemo at decision points — what you decided and why
- Do not wait until session end to save

At session end:
- Save a final summary: accomplishments, decisions, open items
- Update the brain file with current state
```

### Any LLM via REST (no MCP)

If your LLM doesn't support MCP but can make HTTP calls, Mnemo Cortex 
exposes a REST API. The relevant endpoints are:

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Is the server up? |
| `/writeback` | POST | Save a memory (summary + key_facts) |
| `/context` | POST | Recall + search (returns relevant memories) |
| `/preflight` | POST | Pre-task context bundle (recall + brain file) |

**Save (writeback) shape:**
```json
POST /writeback
{
  "summary": "Decision: chose X because Y",
  "key_facts": ["fact one", "fact two"],
  "agent_id": "your-agent-id",
  "session_id": "session-uuid",
  "projects_referenced": [],
  "decisions_made": []
}
```

**Recall (context) shape:**
```json
POST /context
{
  "query": "what you want to find",
  "agent_id": "your-agent-id",
  "max_results": 5
}
```

Add these instructions to whatever system prompt or instruction file 
your platform uses:

```
You have persistent memory via Mnemo Cortex REST API at MNEMO_URL.

Before starting work:
- POST /context with your current task topic to recall prior decisions

When you make a decision or complete something:
- POST /writeback immediately with summary and key_facts

At session end:
- POST /writeback with a session summary
- Update your brain file (separate from the API)
```

> Cross-agent search and brain-file tools are MCP-only today. If 
> you need them via REST, the bridge in `integrations/openclaw-mcp/` 
> is a small Node service you can run as a translator.

---

## Setting Up Your Brain Repo

Your brain file is a markdown file in a Git repo. Fork the 
[mnemo-plan](https://github.com/GuyMannDude/mnemo-plan) template 
to get started, or create your own with this structure:

```
my-project-brain/
  agent-name.md    — your agent's identity, priorities, current state
  active.md        — what's in progress, what's blocked, what's next
  projects/
    project-a.md   — shared project state (any agent can update)
    project-b.md   — shared project state
```

**agent-name.md** — One per agent. Contains: who this agent is, what 
it's working on, current priorities, open threads. Rewritten at 
session end, not appended to.

**active.md** — Shared task board. What's in progress, what's 
completed, what's blocked. Any agent working on the project reads 
and updates this.

**projects/** — One file per project when you have multiple. Status, 
decisions, architecture notes. Shared across all agents on that 
project.

Point your Mnemo MCP config at this repo via the `BRAIN_DIR` env var:

```bash
# In your MCP config or systemd unit:
BRAIN_DIR=/home/you/my-project-brain/brain
```

The bridge auto-enables brain-file tools (`read_brain_file`, 
`write_brain_file`, `list_brain_files`) when `BRAIN_DIR` points at an 
existing directory. If the dir doesn't exist, those tools simply 
don't register — no config errors, no install friction.

Git gives you version history, branching, and backup for free. Push 
at session end, pull at session start. Your agent's context transfers 
between machines, between sessions, between LLMs.

---

## Auto-Capture: What It Does and Doesn't Do

If your Mnemo integration includes auto-capture (MCP bridge watcher 
or sync service), understand what it covers:

**What auto-capture records:**
- Which Mnemo tools were called and when
- Raw session activity (periodic flush)

**What auto-capture does NOT record:**
- Why you made a decision
- What you concluded from your analysis
- What you tried and ruled out
- Strategic context that matters for next time

Auto-capture is the safety net — it ensures nothing is completely 
lost. But it produces low-signal, hard-to-search memories. The 
high-signal memories that make the next session productive come from 
explicit `mnemo_save` calls.

Think of it this way: **auto-capture is the security camera. Manual 
saves are the incident report.** Both exist. Only one is useful when 
you need to understand what actually happened.

---

## Multi-Agent Memory

When multiple agents share a Mnemo Cortex instance, each agent has 
its own memory lane (identified by `agent_id`). Agents search their 
own memories by default. To enable cross-agent search for a session, 
call `mnemo_share` with `enable: true` — then `mnemo_search` accepts 
an `agent_id` argument to target a specific peer (or omit it to search 
all agents). The share toggle is per-session so privacy is opt-in.

**How to structure multi-agent memory:**

- Each agent gets a unique `agent_id` (e.g., `builder`, `strategist`, 
  `support-bot`)
- Each agent has its own brain file (`builder.md`, 
  `strategist.md`)
- Shared project files live in `projects/` where any agent can 
  read and update
- Use `mnemo_search` with a specific `agent_id` to find what another 
  agent did: "What did the builder ship yesterday?"

**Rules for shared memory:**

- Save to your own lane. Read from anyone's.
- Don't overwrite another agent's brain file.
- Shared project files (`projects/*.md`) are fair game for any agent 
  to update — but note who changed what.

---

## Common Mistakes

**Saving every message.** Floods the index with noise. Save outcomes 
and decisions, not process. "We discussed three options" is noise. 
"We chose option B because of latency constraints" is signal.

**Using the brain file as a diary.** The brain file is current state, 
not history. Don't append timestamped entries forever — rewrite 
sections to reflect what's true now. History belongs in Mnemo memory.

**Saving without key_facts.** The `summary` field is narrative. The 
`key_facts` array is for searchable, atomic claims. Always include 
3–5 concrete facts. A save with only a summary is harder to find.

**Never recalling before working.** If you skip recall, you'll 
re-investigate solved problems and contradict prior decisions. Recall 
is cheap. Re-work is expensive.

**Trusting auto-capture alone.** Auto-capture records tool usage, not 
reasoning. The memories that help the next session start smart come 
from your explicit saves. Auto-capture is backup, not primary.

**Treating all memory as equal.** A memory that says "deployed v2.3 
to production, rollback plan at /docs/rollback.md" is worth ten 
memories that say "looked at the deployment script."

---

## Quick Reference

| Action | Tool | When |
|---|---|---|
| Check prior work | `mnemo_recall` | Session start, before any task |
| Find another agent's work | `mnemo_search` | When collaborating |
| Record a decision | `mnemo_save` | Immediately when it happens |
| Record session wrap-up | `mnemo_save` | Session end |
| Load current state | `read_brain_file` | Session start |
| Update current state | `write_brain_file` | Session end |
| Check health | `GET /health` | Troubleshooting |

---

*This guide ships with [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex). 
For installation, see the main README. For the brain repo template, 
see [mnemo-plan](https://github.com/GuyMannDude/mnemo-plan).*
