# The Lane Protocol

> The operating practice that goes with [mnemo-plan](README.md) and [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex). The structure tells you what files to have. This tells you how to live in them.

> 💡 **Paste this into your agent.** This document works as a boot file or system-prompt include. The protocol works whether the human follows it, the agent follows it, or both. Feeding it to your agent means the ritual runs even when you forget.

---

## The pivot

The single thing that makes a fresh session walk in oriented is **a named lane file you always read first**. Not Mnemo. Not the whole brain repo. One file: `agent-lanes/<your-name>.md`.

Your lane is a letter from last-session-you to next-session-you. It says what shipped, what you decided, what you queued, what tripped you up. Without it, a cold start means parsing thirty brain files trying to figure out where you left off. With it, you read one file and you're in.

If you skip every other section of this doc, keep this: **write a lane. Read it first. Update it last.**

---

## The ritual

Every session, same six steps:

1. **Pull.** `git pull` your brain repo. Pick up anything other agents or other machines wrote since you last touched it.
2. **Read your lane + `active.md`.** Your lane is identity and continuity. `active.md` is the project-level "what's in flight." Together they orient you in 60 seconds.
3. **Read task-specific files only as needed.** Debugging? `incidents.md`. Touching infra? `stack.md`. New feature? `project.md` + `decisions.md`. Don't read everything every time. The brain is a reference, not a manifesto.
4. **Work.** Normal session. Tools, edits, conversation.
5. **Write back what changed.** Mark completed tasks done in `active.md`. Update `incidents.md` if you found a new bug or trap. Update `stack.md` if config changed. Save the *why* of non-obvious decisions to Mnemo with `mnemo_save`. Update your lane last — bump the date, note what changed.
6. **Commit + push.** `bash scripts/sync.sh` or `git add -A && git commit -m "brain: ..." && git push`. If you skip the push, the brain is stale.

Six steps. No tools required beyond Git, an editor, and the Mnemo MCP bridge.

---

## Three jobs, three places

Each layer has one job. Don't put the wrong thing in the wrong place.

| Layer | Job | What lives here |
|---|---|---|
| **Brain files** (mnemo-plan) | Current truth | What's true *now*. Rewrite in place. State, not history. |
| **Mnemo** (Mnemo Cortex) | The why | Decisions and the reasoning behind them. Gotchas. Scars. Things that won't survive in `git log`. |
| **Your lane** (`agent-lanes/<name>.md`) | Continuity | Where you left off. Open threads. What next-session-you needs to walk in oriented. |

**When brain and Mnemo conflict, brain wins.** Brain is *now*; Mnemo is *then*. Memories decay. The repo is the source of truth.

---

## What to save where

The hardest discipline is knowing where each thing goes. Heuristics:

- **Finished a task** → mark done in `active.md`. Don't write it up in Mnemo.
- **Made a non-obvious decision** → save the *why* to Mnemo. Update `decisions.md` if it's load-bearing.
- **Found a bug or a gotcha** → write it in `incidents.md`. Save a short Mnemo memory if the pattern is reusable.
- **Changed infrastructure** → update `stack.md`. Mnemo gets *why we changed it*, not the change itself.
- **Closed a session** → bump the date in your lane, note what changed.
- **Long-running thread** → add it to your lane's "Open threads" section. The lane is where threads live until they close.

If you find yourself writing the same thing in two places, you're probably doing it wrong. Pick one.

---

## Multi-agent: same protocol, different tooling

Multiple agents can run the same protocol. They look different on the outside because their tools differ:

| | Reads at session start | Writes at session end | Automatic capture |
|---|---|---|---|
| **Terminal agent** (Claude Code, Cursor, Aider) | `active.md` + own lane + task-specific files | Own lane, `incidents.md`, `active.md` done-marks. `mnemo_save` for the why. | Optional: a sync service that POSTs session activity to Mnemo. |
| **Chat agent** (Claude Desktop, ChatGPT, Gemini) | Own lane + `active.md` + project specs. Manual reads via MCP. | Authors strategy/architecture brain files. Owns own lane. Saves strategy decisions to Mnemo. | None — manual. |
| **Worker agent** (OpenClaw, Agent Zero, n8n) | `mnemo_recall` + brain via `read_brain_file` MCP. | `mnemo_save` manually. Rarely touches brain files directly. | MCP bridge `captureCall` hooks fire on every tool use. |

Same six steps. Different surfaces. The protocol is what's portable.

For solo use with one agent, you don't need `agent-lanes/` — root files are enough. Add lanes when you have a second agent or a second machine, because that's when continuity-across-sessions becomes continuity-across-actors.

---

## Why a lane beats a long memory

Memory systems (Mnemo included) are great at *recall under query*. They're not great at *orientation under cold start*. When you walk into a session, you don't know what to query yet. A lane file is the cheat sheet: it tells you what you don't know to ask about.

The lane is short — one or two screens. It's not a diary. It's the current operating context, rewritten each time. Yesterday's lane is in `git log`; today's lane is the file. Your lane should answer:

- Who am I, and what's my role here?
- What did I just do?
- What did I decide, and why does it matter?
- What's queued for next session?
- What scars / gotchas should I not relearn?

If your lane is longer than two screens, you're probably journaling. Trim it.

See [`agent-lanes/EXAMPLE-AGENT.md`](agent-lanes/EXAMPLE-AGENT.md) for a starter template.

---

## Why this works

It works because it converts a memory problem into a file problem. Memory is fuzzy and probabilistic; files are deterministic. `git pull` gives you the same bytes every time. Read the lane and you know the state.

Mnemo amplifies this: when you need *why* something is the way it is, query Mnemo. When you need *what's true now*, read the brain files. When you need *where you left off*, read your lane. Three questions, three certain answers.

The discipline cost is low. Six steps, ~5 minutes of writebacks per session. The payoff is enormous: every session starts oriented. No drift. No "what was I doing again?"

---

## Failure modes

The protocol breaks in three predictable ways:

1. **You skip the push.** The brain on disk is updated but other agents / other machines never see it. Always commit + push at session end.
2. **You write the same thing in two places.** Brain and Mnemo start to disagree. Resolve by picking the source layer (current truth = brain; reasoning history = Mnemo) and removing the duplicate.
3. **Your lane becomes a diary.** You append session entries instead of rewriting state. The lane bloats; cold-start orientation slows. Rewrite. Old entries are in `git log`.

If your sessions feel less oriented over time, one of those three is happening.

---

## The trinity

- **[Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex)** — memory store. The *why*.
- **[mnemo-plan](README.md)** — brain layout. The *what*.
- **The Lane Protocol** — session ritual. The *how*.

Each layer has a job. Skip any one and the system gets weaker. Run all three and a fresh session walks in oriented every time.

---

*Part of the [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) ecosystem by [Project Sparks](https://projectsparks.ai). MIT licensed — fork it, edit it, make it yours.*
