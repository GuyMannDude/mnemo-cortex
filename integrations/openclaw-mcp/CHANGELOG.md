# Changelog

> **Note on version history:** The bridge tracks the main `mnemo-cortex`
> package version (currently 2.6.4). Versions between 2.0.1 and 2.6.4
> shipped server-side and tooling changes (Dreaming, WikAI, Sparks Bus,
> Developer's Passport, new host integrations) that didn't materially
> change bridge behavior — the bridge continued to work unchanged through
> those releases. The full history is in the main repo
> [CHANGELOG.md](../../CHANGELOG.md).

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
- Auto-sync watchers (e.g. `mnemo-cc-artforge-sync`) must now tag their
  writes as `source: "tool", category: "session_log"` so they're hidden
  from default recalls. Update bundled in this release.

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
  (`April`, `Peter`). The month "April" and the common name "Peter"
  produced false-positive `relationship` tags on records that had
  nothing to do with collaborators. Domain keywords (`hoffman`,
  `customer`, `client`, `collaborator`, `merchant`) remain.

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

Full spec: `mnemo-v3-provenance-spec.md` in the Sparks brain.

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
