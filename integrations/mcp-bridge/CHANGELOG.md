# Changelog

> **Note on version history:** The bridge used to track the main
> `mnemo-cortex` package version step-for-step. That coupling loosened
> once the main package added features the bridge didn't need to
> change for — Phase 3 Facts wired through as a thin passthrough,
> the Mem0 retirement was server-side only. Bridge currently at 2.10.0;
> main package at 3.1.0. Versions between 2.0.1 and 2.6.4 shipped
> server-side and tooling changes (Dreaming, WikAI, Sparks Bus,
> Developer's Passport, new host integrations) that didn't materially
> change bridge behavior — the bridge continued to work unchanged
> through those releases. The full history is in the main repo
> [CHANGELOG.md](../../CHANGELOG.md).

## 2.15.0 — 2026-07-05 — Lane-staleness nag: the boot block measures Lane Protocol compliance

**Problem:** The Lane Protocol's "update your own lane file every session" step lived
only in tool descriptions and the boot-block ritual text — passive instructions no
agent re-reads. Compliance audit (Guy, 2026-07-05): only cc's lane was current;
opie.md hadn't had a real update since 2026-05-08 (~6 weeks), rocky.md ~3.5 weeks,
dave-session.md ~2.5 weeks, cody-session.md never since onboarding. Every agent was
coordinating off the others' stale reality, silently.

**Fix:** Two active signals, both from git truth (`git log -1 --format=%ct -- <lane>`):
(1) `agent_startup` prepends a `⚠️ YOUR LANE FILE IS STALE` banner — with the age in
days — whenever the lane's last commit is older than 7 days; it fires every boot until
the lane gets a commit. (2) `session_end` appends an advisory when the lane's last
commit predates this bridge process's start (i.e., the session that is ending never
touched it), telling the agent exactly what to call. Both checks are try/caught —
a failed git probe never breaks a boot or a session end.

## 2.14.0 — 2026-07-05 — Dream brief fetched from the Cortex, not local disk

**Problem:** `agent_startup` read the dream brief from `DREAM_DIR` on the
machine running the bridge, inside a silent catch. The dreamer writes dreams
on the Cortex host — since the dreamer moved off the agents' machine, the
bridges' `~/.agentb/dreams` never existed and every boot silently skipped the
DREAM BRIEF section. (Misdiagnosed in the field as a `/context` timeout.)

**Fix:** The dream section now asks the server first — `GET /dream/latest`
(new in mnemo-cortex v4.9.3) — and only falls back to the local `DREAM_DIR`
read when the server is unreachable or predates the endpoint. The 48h
freshness gate applies on both paths.

## 2.13.0 — 2026-07-02 — Creative harness: `idea` category + recall mode=explore

**Problem:** The creative-harness audit (bus #1003) found the bridge's category
enums had no home for creative content — an idea seed could only be filed as
`decision` or fall into hidden `session_log` — and recall had exactly one lens:
best-match-plus-recency, which buries the half-forgotten connection that
creative recall lives on.

**Fix:** (1) `idea` added to the category enum on `mnemo_recall`, `mnemo_search`,
and `mnemo_save` (server v4.8.0 counterpart: perpetual decay, 0.85 ranking
prior, classifier + regex support). (2) New optional `mode` param on
`mnemo_recall`: `focus` (default, unchanged) or `explore` — the serendipity
lens: prefers the similarity band adjacent to the top hit, ignores recency,
favors rarely-recalled memories. Use `mode=explore` when brainstorming.

## 2.12.0 — 2026-06-25 — Trajectory tools: mnemo_save_trajectory + mnemo_recall_trajectory

**Problem:** The bridge exposed memory save/recall but not the new v4.5 trajectory-learning
endpoints, so agents had no tool to capture or recall a proven task recipe.

**Fix:** Two new tools wrapping the server's `/trajectory/save` and `/trajectory/recall`:
- `mnemo_save_trajectory` — agent calls it AFTER a task succeeds with the ordered steps,
  outcome, and a 1–5 self-rating (POST `/trajectory/save`, `agent_id` = this agent).
- `mnemo_recall_trajectory` — agent calls it BEFORE a task with an NL query; returns the
  nearest recipes (similarity → rating → recency) rendered as readable numbered recipes via a
  new `formatTrajectory` helper. Honors `task_type` and `min_rating` (default 3).

Both surface ambient `captureCall` like the existing tools. No change to the memory tools.
Bridge 26 tools total (was 24).

## 2.11.1 — 2026-06-18 — Auto-pull works when the brain dir is a repo subdir

**Problem:** The startup `agent_startup` git-pull was gated on
`existsSync(join(BRAIN_DIR, ".git"))` — it only pulled if `.git` sat
*directly inside* `BRAIN_DIR`. But the brain dir is commonly a **subdir** of
its repo: the shared `sparks-brain-guy/brain` layout (`.git` at the repo
root) and the documented mnemo-plan default `~/mnemo-plan/brain` both put the
`.md` files one level below `.git`. For those, the check returned false and
the pull was silently skipped (`pullStatus = "skipped (no .git)"`), so the
agent read whatever stale snapshot was on disk. It went unnoticed because the
interactive IGOR agents refresh the clone via a manual session-ritual `git
pull`; a daemon agent (Dave, migrated onto the shared brain 2026-06-18) has no
such ritual and so never auto-refreshed at all.

**Fix:** Detect the work tree the way git itself does — walk up the tree with
`git rev-parse --is-inside-work-tree` (cwd = `BRAIN_DIR`) instead of looking
for a literal `.git`. `git pull --ff-only` then runs from the subdir fine
(it's a repo-level operation regardless of cwd). A non-repo brain dir now
reports `skipped (not a git repo)`; a real pull failure still reports
`FAILED (...)`. Verified across a repo subdir (was false → now pulls), a repo
root (unchanged), and a non-git dir (correctly skips, no false FAILED).
Commands are constant literals — no shell interpolation, no injection surface.

> History note: 2.11.0 (capture pause/resume, see main CHANGELOG) bumped the
> server version string but never got an entry here — pre-existing gap, noted
> not back-filled.

## 2.10.1 — 2026-06-07 — Stop auto-capture from duplicating manual saves

**Problem:** `mnemo_save` was set to `"full"` in the `TOOL_CAPTURE` policy
map, so every deliberate save was *also* echoed into the auto-capture ring
buffer and flushed back as a separate `[AUTO-CAPTURE]` chunk. The same fact
ended up stored twice — once clean, once wrapped in tool-call narration —
and the duplicate competed for the same top-k slots on recall. A composition
audit of CC's store (2,475 chunks, 2026-06-07) found ~5% (133 chunks) were
these `[AUTO-CAPTURE]` echoes of manual saves, plus 30 empty
`auto_capture_flush` blanks — pure recall dilution.

**Fix:** `mnemo_save: "full"` → `"skip"` in `TOOL_CAPTURE`. The save still
persists via its own handler; only the redundant auto-capture echo is
dropped. `captureCall("mnemo_save", …)` at the top of the handler is left in
place — it still runs `trackCall()` (memory-nudge accounting) and now returns
early at the policy gate, so nudge behavior is unchanged. Reads
(`mnemo_recall`/`mnemo_search`) and `write_brain_file` keep their capture
policies — those are legitimate activity-trail entries, not self-duplication.

Pre-existing duplicate `[AUTO-CAPTURE]` chunks are not retroactively purged
by this change; a separate dedup sweep can handle the backlog.

## 2.10.0 — 2026-05-23 — Phase 3 Facts tools + host-local session IDs

Two changes that had piled up under `version: "2.9.0"` in `package.json`
without a further bump, now lifted into a proper release. No new code
in this commit — just `package.json` 2.9.0 → 2.10.0 and the matching
`McpServer` version constant in `server.js`. The features themselves
landed on 2026-05-19 (host-local session IDs) and 2026-05-20 (Phase 3
Facts bridge tools); the version bump just catches up.

### Phase 3 — four Facts tools wired through the bridge (2026-05-20)

Bridge passthroughs for the Phase 3 Facts HTTP routes added in the main
package. Same provenance/audit story, exposed to every MCP host that
spawns the bridge.

- `mnemo_fact_get(entity, attribute, include_false?)` — single lookup,
  human-formatted output, `{found: false}` when missing.
- `mnemo_fact_query(entity?, attribute?, value_contains?, confidence?, limit?)`
  — filtered list.
- `mnemo_fact_save(entity, attribute, value, confidence, evidence_source, source_memory_id?)`
  — UPSERT with the promotion ladder enforced server-side; `isError: true`
  when the contradiction algorithm rejects a write.
- `mnemo_fact_demote(entity, attribute, reason)` — explicit
  `verified → false` transition for "this is wrong but I don't know the
  correct value yet."

Each tool calls `captureCall()` for auto-capture parity with the existing
memory tools. `source_agent` auto-populates from `AGENT_ID`. Tool
descriptions teach the `evidence_source` prefix convention
(`memory:<id>`, `commit:<sha>`, `statement:<who>`, etc.).

`readOnlyHint` matrix: `get`/`query` read-only, `save`/`demote` mutate.
`demote` carries `destructiveHint: true` because it's an explicit
assertion that an existing value is wrong.

### Session IDs in host-local time (2026-05-19)

`sessionId` used to come from `new Date().toISOString()`, which is UTC.
Every other Sparks timestamp (active.md, brain commits, kickstart
filenames) is host-local, so after 17:00 PT the bridge would write
session IDs dated "tomorrow" while the rest of the brain said today.
Added `localTimestamp()` + `localDateOnly()` helpers near the sessionId
generator and replaced the four UTC-derived call sites (mnemo_save
fallback, session header writes).

## 2.9.0 — 2026-05-15 — Developer Dump (Mnemo v4 Phase 1)

**A bridge-level JSONL trace of every MCP tool call your agents make.**
Catches the silent-tool-failure class that hid Peter Widget's outage
— a tool that returns `{isError: true}` without throwing looks
identical to a successful call from every layer above the bridge.
Off by default; flip on with `MNEMO_DUMP=on`.

### What lands on disk

One JSONL file per agent per day at
`~/.mnemo-cortex/dumps/<agent_id>/<YYYY-MM-DD>.jsonl`. Each line:
`tool`, full `params`, full `response`, `latency_ms`, `ok`, and an
`error` field on failures. Greppable with `jq`:

```bash
jq 'select(.ok == false) | {tool, error, latency_ms}' \
  ~/.mnemo-cortex/dumps/rocky/$(date -u +%F).jsonl
```

### How it wires up

Monkey-patches `server.registerTool` once at the `McpServer` level so
all 18 then-existing tools (and every future tool, including the
Phase 3 Facts additions above) are covered by a single diff. When
`MNEMO_DUMP=off` (the default) `dump.wrap()` returns the original
handler unchanged — no allocation, no overhead.

Captures both real thrown errors and the handler-internal
`{isError: true}` returns. Schema-versioned for future additions.

### CLI

Surfaced through the main `mnemo-cortex` binary, not the bridge:

```bash
mnemo-cortex dump list           # all dump files, size + line count
mnemo-cortex dump tail rocky     # live-tail today's rocky dump
```

### Tests

`integrations/mcp-bridge/dump.test.js` covers off-mode no-op, on-mode
header+event, two-agent isolation, day rollover, write failure,
successful capture, `isError` capture, thrown-error capture, disabled
passthrough, and `listDumps()`.

### Package metadata

- `package.json` `version`: 2.8.1 → 2.9.0.
- `server.js` McpServer version constant bumped to match.
- Main package `pyproject.toml` + `cli.py` aligned to 2.9.0 as well
  (alignment drift between bridge / cli / py-package caught up in
  this release).

### Scope

Captures only MCP tool traffic the bridge sees. Raw Claude API
exchanges, message-level capture, and content filters need per-agent
hooks — that's Mnemo v4 Phase 1.5.

## 2.8.1 — 2026-05-13 — Rename: `openclaw-mcp` → `mcp-bridge`

**Rename-only release. No functional change.** The directory hosting
this code moved from `integrations/openclaw-mcp/` to
`integrations/mcp-bridge/`. The old name was a leftover from when this
bridge was OpenClaw-specific; the code has long since been the generic
bridge that every Mnemo Cortex integration (Claude Desktop, Claude
Code, OpenClaw, LM Studio, AnythingLLM, Agent Zero, Hermes Agent,
Ollama Desktop, Open WebUI, llama.cpp, LobeChat, Jan) spawns on
stdio. The new path tells the truth.

### Migration

- **Existing user configs** that point at `…/integrations/openclaw-mcp/server.js`
  keep working — there's a symlink at the old path resolving to
  `../mcp-bridge/server.js`. Update your MCP client config to the new
  path when convenient.
- **Fresh installs** (anyone following the README or running
  `robot-install.sh` after this commit) see only the new path. No
  action required.
- **Windows users without symlink support** (most Git for Windows
  installs handle them, but stricter configs may not): update your
  MCP client config to point at `integrations/mcp-bridge/server.js`
  directly. The symlink fallback won't resolve for you.

### Package metadata

- `package.json` `name`: `mnemo-cortex-openclaw-mcp` → `mnemo-cortex-mcp-bridge`.
- `package.json` `version`: 2.8.0 → 2.8.1.
- `server.js` McpServer version constant bumped to match.

### Future deprecation

The back-compat symlink at `integrations/openclaw-mcp/` is kept for
existing users to migrate at their own pace. It will be removed in a
future major version; the deprecation notice is in
`integrations/openclaw-mcp/README.md`.

## 2.8.0 — 2026-05-13 — Mnemo Cortex v3: Provenance & Decay

**The agent's own inference is no longer indistinguishable from a verified
fact.** Mnemo records now carry where the fact came from (`source`) and what
kind of fact it is (`category`). Topology / current-state facts decay; old
ones surface a structured `stale_warning` on recall — programmatic agents
branch on the field instead of trusting a 90-day-old IP.

### Added — `mnemo_save` provenance fields (all optional)

- `source`: `user | tool | inferred | brain | migrated`. Defaults to
  `inferred`. Set to `user` when the operator stated the fact directly,
  `tool` for deterministic outputs, `brain` when pulled from a brain file.
- `category`: `topology | current_state | doctrine | incident | identity |
  relationship | decision | session_log | unknown`. Drives decay behavior.
  When omitted, the bridge's regex auto-suggester picks a category and
  returns its choice + matched keywords in the save response so the agent
  can learn the conventions.
- `additional_tags`: free-form human-readable tags for search.

The save response gains `category_used`, `category_suggested`,
`category_match_keywords`, `source_used` so the caller sees what the server
actually stored.

### Added — `mnemo_recall` / `mnemo_search` filters

- `source`: restrict to one provenance source (e.g., highest-confidence
  `user` / `tool` only).
- `category`: restrict to a single category.
- `exclude_categories`: drop categories from results. Defaults to
  `["session_log"]` — auto-sync watcher noise is hidden from default
  recalls. Pass `[]` to include everything.
- `exclude_stale`: drop topology records past 1.5x their warn threshold.
- `max_age_days`: hard age cap.

### Added — structured `stale_warning` field on every returned chunk

When a record exceeds its category's warn threshold, the chunk carries:

```json
{
  "stale_warning": {
    "category": "topology",
    "age_days": 95.0,
    "threshold_days": 30,
    "severity": "stale",
    "message": "TOPOLOGY fact from 2026-02-07 (95 days old). Verify with a tool call before acting."
  }
}
```

Tool-result rendering inlines a `⚠️ STALE: …` banner so agents under
context pressure can't miss it. The structured field is the contract;
programmatic agents must do `if (chunk.stale_warning) { verify_first() }`
before acting on aged topology facts.

### Decay thresholds (defaults; override per-deployment)

| Category | Warn | Stale | Default visibility |
|---|---|---|---|
| `topology` | 30d | 90d | visible |
| `current_state` | 90d | — | visible |
| `relationship` | 180d | — | visible |
| `session_log` | 90d | — | **hidden** by default |
| `unknown` | 90d | — | visible (decays like current_state) |
| `doctrine`, `incident`, `identity`, `decision` | perpetual | — | visible |

Override via bridge env vars: `MNEMO_DECAY_TOPOLOGY_WARN_DAYS`,
`MNEMO_DECAY_TOPOLOGY_STALE_DAYS`, `MNEMO_DECAY_CURRENT_STATE_WARN_DAYS`,
`MNEMO_DECAY_RELATIONSHIP_WARN_DAYS`, `MNEMO_DECAY_SESSION_LOG_WARN_DAYS`.

### Bridge / migration

- New migration script `agentb-bridge/migrations/v3_provenance.py`. Two
  phases, idempotent:
  - **Phase 1** — base-tag every record `source=migrated, category=unknown,
    schema_version=3`.
  - **Phase 2** — regex topology rescue. Re-tags any record whose summary
    or key_facts match the topology regex as `category=topology` with
    `provenance_note=auto_categorized_topology_regex_v3_migration`.
- The migration regex is the same pattern used by the write-time
  auto-suggester — single source of truth. Re-running the script touches
  zero records on a second pass.
- Any auto-sync watcher (a periodic process that batches session
  activity to Mnemo) must now tag its writes as `source: "tool",
  category: "session_log"` so the mechanical noise stays hidden from
  default recalls.

### Bridge internal writebacks — auto-tagged

The bridge fires its own writebacks on auto-capture flush, session
start, and session end. Pre-v3 these were untagged and would land
indistinguishable from agent inference. v3 tags them at the source:

- **Auto-capture flush** (`[AUTO-CAPTURE]` payloads, fires every 8
  tool calls or 2-min idle): `source: "tool", category: "session_log"`.
- **Session-start marker** (fired by `agent_startup` / `opie_startup`):
  `source: "tool", category: "session_log", additional_tags:
  ["session_start"]`.
- **`session_end` summaries** (user-authored recap): `source: "user",
  category: "current_state", additional_tags: ["session_end"]`. The
  recap is a real fact, not session noise — but it's "what's in flight
  this session" so it decays like current_state. Bypasses the regex
  auto-suggester to avoid keyword false-positives (e.g., the word "bug"
  in a debug narrative).

### Regex auto-suggester refinements

- Reordered `PROVENANCE_PATTERNS` so `decision` runs before `incident`.
  *"Decided to ship after fixing the bug"* now correctly classifies as
  `decision`, not `incident`. Decision verbs are more diagnostic than
  failure nouns when both appear in the same record.
- Narrowed the `relationship` regex to drop bare first-name matches
  that collided with calendar months and common English given names.
  Those patterns produced false-positive `relationship` tags on records
  that had nothing to do with collaborators. Configure your own
  collaborator/client keywords per deployment — the default ships with
  generic role terms (`customer`, `client`, `collaborator`, `merchant`)
  only.

### Backward compatibility

- Old clients that don't send v3 fields still work — bridge applies safe
  defaults (`source=inferred`, regex-suggested category).
- Pre-v3 records returned by recall surface `provenance_source: null`,
  `category: null`, `stale_warning: null`. Code that branches on
  `stale_warning` presence Just Works.
- The new MCP tool params are all optional; existing callers see no
  change in behavior unless they opt in.

### Reasoning

This is the fix for "the agent can store its own inference or previous
run as a confirmed fact, which could in turn influence future runs to get
quietly worse" — the failure mode Nate B Jones names in his SAP/Dreamio
analysis. Pine Cone Nexus and SAP Dreamio bake the same idea (provenance,
freshness, confidence) into enterprise retrieval contracts. v3 brings it
to personal/small-team scale.

## 2.7.0 — 2026-05-03

**Added:** `agent_startup` tool — neutral, agent-aware session boot. Loads the
lane file matching `MNEMO_AGENT_ID` (`<id>.md`, falling back to
`<id>-session.md`), the cross-agent operating docs (`CLAUDE.md`, `active.md`,
`people.md`, `doctrines.md`), recent Mnemo memories scoped to the calling
agent, and the latest dream brief if recent. Returns an agent-neutral header —
identity stays in the agent's system prompt; the bridge provides continuity,
not identity.

**Deprecated:** `opie_startup` is now a thin alias that forces `agent_id="opie"`
and loads `opie.md` regardless of `MNEMO_AGENT_ID`. Behavior preserved
bit-for-bit for existing Opie / Claude Desktop installs. Description updated
to point at `agent_startup`. Will be removed in a future major version.

**Problem:** The original `opie_startup` was hardcoded to load `opie.md` and
return Opie's identity prompt regardless of who called it. Tool description
read *"CALL THIS FIRST in every new conversation"* which any agent would obey
on session start. Result: a non-Opie agent (e.g. Rocky on Hermes) auto-called
`opie_startup`, got handed Opie's identity, and proceeded to roleplay Opie.
The bridge's own source comment acknowledged the footgun: *"Other agents can
call it but will get an Opie-shaped orientation."*

**Why this matters publicly:** the bridge ships in
`mnemo-cortex/integrations/openclaw-mcp/` and is the same code every install
spawns. Any new user who set `MNEMO_AGENT_ID=their-agent` and let their agent
auto-call the "CALL THIS FIRST" tool got an Opie identity instead of their
own. With 2.7.0 the bridge is **blank-slate by default** — agents see
`agent_startup` first and load their own lane based on their configured
`MNEMO_AGENT_ID`.

**Migration:** existing Opie installs need no changes — `opie_startup` keeps
working with original behavior. Any system prompt or doc that explicitly
references `opie_startup` continues to work. For new agents, point at
`agent_startup` and ensure `MNEMO_AGENT_ID` is set to a value matching a `.md`
file in your `BRAIN_DIR`.

## 2.6.4 — 2026-04-28

**Fixed:** Silent crash diagnostics. Bridge now logs cause when it exits.

**Problem:** Two unexplained disconnects in Claude Desktop on 2026-04-28 (07:03 and 07:59 UTC) left no trace in the MCP log — `Server transport closed unexpectedly` with empty stderr. Bridge auto-recovers, but root cause was undiagnosable.

**Fix:** Added handlers for `uncaughtException`, `unhandledRejection`, `process.exit`, `SIGHUP`, `SIGPIPE`, and `stdin` EOF. The next crash writes its cause (stack trace, signal name, or exit code) to stderr, which Claude Desktop captures into `mcp.log`.

## 2.0.1 — 2026-03-29

**Fixed:** Agent context overflow from unbounded search results. `formatChunks()` now caps total response size to prevent large memory recalls from exceeding the agent's context window. Default max_results reduced from 5 to 3.

**Problem:** Agents with smaller context windows (e.g. DeepSeek V3.2 at 131K) would overflow when mnemo_recall or mnemo_search returned multiple large L2 memory chunks. A single search could dump 25K+ tokens into context.

**Fix:** Response output is now capped at 16K characters (~4K tokens). When results exceed the cap, remaining matches are noted with a truncation message. Agents can narrow their query for more detail.

## 2.0.0 — 2026-03-29

**Added:** Share switch — three-level cross-agent sharing control (separate/always/never) with per-session toggle via mnemo_share tool. Privacy-first: sharing off by default.

**Fixed:** All findings from CC self-review and AL independent security audit — 10-second fetch timeout, ensureHealth() retry pattern, zod declared as dependency, string length limits, error message sanitization, Node.js engines field, test defaults, failure-case tests.
