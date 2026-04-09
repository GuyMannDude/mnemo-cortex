# Changelog

## v2.3.1 — "Total Recall" (2026-04-08)

Documented auto-capture and added the `MNEMO_AUTO_CAPTURE` environment variable gate.

### What's New

- **Auto-Capture documentation** — New README section covering the two capture patterns (OpenClaw/Claude Code session watcher, Claude Desktop MCP bridge), quick start, and always-on configuration.
- **`MNEMO_AUTO_CAPTURE` env var** — Set to `true` and `mnemo-cortex start` automatically starts the session watcher. Default: off. No behavior change for existing users.

### Problem This Solves

Auto-capture has been working in production for weeks (CC watcher running 2+ weeks straight, zero failures) but wasn't documented anywhere in the public repo. New users had no idea the feature existed.

---

## v2.3.0 — "The Responsible Thing" (2026-04-07)

Pulled the Claude Desktop MCP bridge until Anthropic's new session storage architecture is supported.

### What Changed

- **Claude Desktop integration removed** — `integrations/claude-desktop/` pulled from the repo. The MCP tools (recall, search, save, startup, brain file read/write) worked correctly, but the automatic session watcher depended on Claude Desktop writing `.jsonl` files to `~/.config/Claude/local-agent-mode-sessions/`. Desktop v2.1.87+ ("cowork VM" architecture) moved session storage to internal IndexedDB/LevelDB. The watcher had nothing to watch.
- **README, CAPABILITIES, health output updated** — All references to the Desktop integration now include a notice explaining the pull and that Claude Code + OpenClaw integrations are unaffected.
- **mnemo-cortex-mcp repo unchanged** — The archived standalone repo already redirects here. Its README still points to this repo as the canonical source.

### Problem This Solves

Anyone following the Desktop setup docs would get a dead session watcher that silently captured nothing. Opie (our own Desktop agent) ran for 13 days with a broken watcher before we caught it. Rather than ship a known-broken integration, we pulled it.

### What's Next

The MCP server itself is fine — the 7 tools work. The gap is automatic session capture. Options being evaluated:
1. Read from Claude Desktop's new LevelDB/IndexedDB storage
2. MCP-only memory persistence (no file watcher needed)
3. Wait for Anthropic to expose a session export API

### Claude Code and OpenClaw users

Nothing changed for you. Your integrations work exactly as before.

---

## v2.2.0 — "One Repo, One Install" (2026-04-04)

Merged the MCP bridge (formerly mnemo-cortex-mcp) into the main repo. One product, one install.

### What's New

- **Built-in MCP bridge** — The Claude Desktop / Claude Code MCP server now lives at `integrations/claude-desktop/`. No separate repo needed. 7 tools: recall, search, save, startup, read/write/list brain files.
- **mnemo-cortex-mcp archived** — The old separate repo redirects here. All existing links still work.

### Problem This Solves

Users had to find and install two separate repos to get memory working. That's broken. Now it's one clone, one install.

### Migration

If you were using `mnemo-cortex-mcp` separately:
1. Pull the latest `mnemo-cortex`
2. Update your MCP config path: `mnemo-cortex-mcp/server.js` → `mnemo-cortex/integrations/claude-desktop/server.js`
3. Run `cd integrations/claude-desktop && npm install`

---


## v2.1.0 — "No Agent Runs Without Verified Memory" (2026-04-04)

Built-in deployment health verification. Auto-discovers agents, tests live recall, validates MCP configs, checks watchers.

### What's New

- **`mnemo-cortex health` command** — Comprehensive deployment health check that auto-discovers every agent from the database and runs live recall tests against each one. No hardcoded agent names.
- **MCP config validation** — `--check-mcp` flag verifies mnemo-cortex is registered as an MCP server in any config file (OpenClaw, Claude Desktop, etc). Catches the exact bug where an agent's MCP pipe is silently broken.
- **Watcher service monitoring** — Auto-discovers all mnemo-related systemd services and reports their status.
- **Multiple output modes** — `--json` for scripts/monitoring, `--quiet` for cron (exit code only), `--agents` for agent-only checks, `--services` for watcher-only checks.
- **CronAlarm integration** — Drop-in compatible with cron alerting. Non-zero exit on any failure.

### Problem This Solves

Rocky's Mnemo MCP tools were missing from his openclaw.json config. Nobody knew until Guy tried to use them — weeks later. This command catches that in 10 seconds, automatically, on a schedule.

### Usage

```
mnemo-cortex health                         # full check, human output
mnemo-cortex health --json                  # machine-readable for scripts
mnemo-cortex health --quiet                 # exit code only (for cron)
mnemo-cortex health --agents                # only test agent recall
mnemo-cortex health --services              # only check watcher services
mnemo-cortex health --check-mcp ~/.openclaw/openclaw.json
mnemo-cortex health http://artforge:50001   # explicit server URL
```

### CronAlarm Example

```
0 */6 * * * mnemo-cortex health --quiet || cronalarm send "Mnemo health failed"
```

### Credits

- **Guy Hutchins** — Doctrine: "No agent runs without verified memory"
- **CC** (Claude Code Opus 4.6) — Implementation

---


## v2.0.0 — "Don't Fear the /new!" (2026-03-17)

Ground-up rewrite. SQLite replaces JSONL. Proven on two live agents with six weeks of unbroken recall.

### What's New

- **SQLite + FTS5 storage** — All memory in a single database with full-text search. No more JSONL files. Fast, portable, zero dependencies.
- **Context frontier with active compaction** — Rolling window of messages + summaries. Older messages are automatically summarized, achieving ~80% token compression while preserving perfect recall.
- **DAG-based summary lineage with source expansion** — Every summary tracks which messages it was built from via a directed acyclic graph. The `summary_sources` table links condensed summaries back to their leaf summaries, creating full traceability from any summary to its original messages.
- **Verbatim replay mode** — Summaries are the default view, but any summary can be expanded back to the original messages for full-fidelity context.
- **OpenClaw session watcher daemon** — Lightweight sidecar that tails JSONL session files and ingests new messages into SQLite every 2 seconds. No hooks, no agent cooperation required.
- **Context refresher daemon** — Writes `MNEMO-CONTEXT.md` to the agent's workspace on a 5-second interval. The agent reads it at bootstrap for instant memory hydration.
- **Provider-backed summarization via OpenRouter** — Compaction summaries generated by Gemini 2.5 Flash via OpenRouter, with deterministic truncation fallback when no API key is available. No local GPU required.
- **Sidecar architecture** — Version-resistant design that observes session files from outside the agent. If Mnemo crashes, the agent keeps working. If the agent crashes, Mnemo already has everything on disk.

### Live Deployment

Proven on two live OpenClaw agents:

- **Alice** (THE VAULT, Threadripper 3970X) — Running since early March 2026
- **Rocky** (IGOR, Ubuntu laptop) — Deployed March 17, 2026. 3,000+ messages ingested, 429+ summaries generated, 20+ conversations tracked. Recall to Day One.

### Breaking Changes

- v2.0 uses a completely new storage backend (SQLite) and does not share data with v1's JSONL/semantic cache system
- The v1 HTTP API (`/context`, `/preflight`, `/writeback`, `/ingest`) is still available via the FastAPI server but is no longer the primary integration path
- The recommended integration is now file-based: watcher daemon → SQLite → refresher daemon → `MNEMO-CONTEXT.md` → agent bootstrap

### Credits

- **Guy Hutchins** — Project lead
- **Opie** (Claude Opus 4.6) — Architecture and schema design
- **AL** (ChatGPT) — Implementation
- **CC** (Claude Code) — Deployment, integration, live testing
- **Alice & Rocky** — Live test subjects

---

## v0.6.0 (2026-03-08)

- FTS5 exact-match recall (credit: AL's claw-recall design)

## v0.5.0 (2026-03-07)

- Refresh command and refresher daemon
- MNEMO-CONTEXT.md workspace injection

## v0.4.0 (2026-03-05)

- Live Wire (`/ingest`) endpoint
- HOT/WARM/COLD session lifecycle
- Session Watcher daemon

## v0.3.0 (2026-03-03)

- Multi-tenant isolation
- Circuit breaker fallback chains
- Persona modes (strict/creative)

## v0.2.0 (2026-02-28)

- Core server with pluggable providers
- Framework adapters (OpenClaw, Agent Zero)
- L1/L2/L3 cache hierarchy
