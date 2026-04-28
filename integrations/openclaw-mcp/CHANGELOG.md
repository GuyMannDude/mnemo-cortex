# Changelog

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
