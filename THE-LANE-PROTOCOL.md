# The Lane Protocol

> The operating practice that goes with [mnemo-plan](https://github.com/GuyMannDude/mnemo-plan) and [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex). The structure tells you what files to have. This tells you how to live in them.

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

## Task structure (optional)

Plain bullets work fine for small lanes:

```markdown
## In Progress
- [ ] Test FrankenClaw against Hermes end-to-end
```

When you have many tasks across multiple agents — or you want agents to coordinate on `active.md` without parsing prose — promote each task to a structured line. The shape comes from how issue trackers let agents coordinate (the pattern [OpenAI's Symphony](https://github.com/openai/symphony) exploits with Linear):

```markdown
- [ ] [task:fc-hermes-test] (cc, state:up-next) Test FrankenClaw ↔ Hermes end-to-end
- [ ] [task:lane-symphony] (cc, state:in-progress, blocks:lane-symphony-public) Symphony-shape upgrade to live brain
- [ ] [task:lane-symphony-public] (cc, state:up-next, blocked-by:lane-symphony) Mirror upgrade to public THE-LANE-PROTOCOL.md
- [x] [task:codex-login-verify] (cc, state:done, 2026-05-01) Verify codex CLI auth persisted
```

### The four fields

| Field | Meaning | Example |
|---|---|---|
| `[task:slug]` | Stable per-task ID. Kebab-case, semantic, lives the life of the task. | `[task:hoffman-gmc-appeal]` |
| `(assignee, ...)` | Who owns it right now. One agent name. Other agents read but won't act. | `(cc)`, `(opie)`, `(rocky)` |
| `state:label` | Where it is in the lifecycle. | `state:in-progress` |
| `blocks:` / `blocked-by:` | Dependency edges. Use the slug of the other task. | `blocked-by:fc-hermes-test` |

### The state machine

```
   ┌─────────┐  claim   ┌─────────────┐ request_review ┌────────┐ complete ┌──────┐
   │ up-next │ ───────▶ │ in-progress │ ─────────────▶ │ review │ ────────▶ │ done │
   └─────────┘          └──────┬──────┘                └────────┘           └──────┘
                               │
                               │ block / park / abandon
                               ▼
                     ┌─────────┬────────┬───────────┐
                     │ blocked │ parked │ wont-fix  │
                     └─────────┴────────┴───────────┘
                          │ unblock
                          └────▶ back to in-progress
```

- **`up-next`** — queued. Nobody's working on it.
- **`in-progress`** — claimed. Active work.
- **`review`** — work done, awaiting human gate or another agent.
- **`done`** — terminal. Stays in `active.md` briefly, then archives.
- **`blocked`** — has `blocked-by:` pointing at another task.
- **`parked`** — paused deliberately. Could resume.
- **`wont-fix`** — terminal-failure. Decided not to do it.

### The verbs

A small vocabulary for what your file edits *mean* — not tools to invoke, just shared words across agents:

| Verb | What changes in the file |
|---|---|
| `claim(slug)` | `assignee` → you, `state` → `in-progress` |
| `transition(slug, state)` | `state:` field updates |
| `block(slug, blocker_slug)` | `state:blocked`, add `blocked-by:` |
| `unblock(slug)` | Drop `blocked-by:`, `state:up-next` |
| `complete(slug)` | `[x]`, `state:done`, optional `(YYYY-MM-DD)` |
| `archive(slug)` | Cut from `active.md` (history stays in `git log`) |

### When to skip this

Solo project with a handful of open tasks? Overkill. Reach for structured tasks when:

- Two or more agents touch the same `active.md` and you've felt the friction.
- You've ever lost track of what was blocking what.
- You want an agent to claim "the next un-assigned `up-next` task" without parsing prose.
- You're building toward a [Symphony](https://github.com/openai/symphony)-shaped pipeline that needs structural state to operate.

The structure is opt-in per task. Untagged bullets remain valid. The convention scales with you.

### Why this shape

It's not a coincidence that this looks like an issue tracker. Bugzilla → Jira → Linear → Symphony is the same pattern at every scale: durable state, explicit ownership, legal transitions, audit history, dependency graph, scoped permissions, structural verbs. Those seven properties are what make a tool **agent-substrate-ready**. When `active.md` has them, agents can operate against it without a wrapper.

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

See [`agent-lanes/EXAMPLE-AGENT.md`](https://github.com/GuyMannDude/mnemo-plan/blob/master/agent-lanes/EXAMPLE-AGENT.md) for a starter template.

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
- **[mnemo-plan](https://github.com/GuyMannDude/mnemo-plan)** — brain layout. The *what*.
- **The Lane Protocol** — session ritual. The *how*.

Each layer has a job. Skip any one and the system gets weaker. Run all three and a fresh session walks in oriented every time.

---

*Part of the [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) ecosystem by [Project Sparks](https://projectsparks.ai). MIT licensed — fork it, edit it, make it yours.*
