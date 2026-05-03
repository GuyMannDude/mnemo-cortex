# Changelog

> **Note on version history:** The bridge tracks the main `mnemo-cortex`
> package version (currently 2.6.4). Versions between 2.0.1 and 2.6.4
> shipped server-side and tooling changes (Dreaming, WikAI, Sparks Bus,
> Developer's Passport, new host integrations) that didn't materially
> change bridge behavior — the bridge continued to work unchanged through
> those releases. The full history is in the main repo
> [CHANGELOG.md](../../CHANGELOG.md).

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
