# Changelog

## v2.6.1 (2026-04-27)

Bridge surfaces agent attribution again. Mnemo Cortex's `/context` endpoint
returns chunks without an `agent_id` field, so the bridge was always
displaying `agent=?` in cross-agent search results. Now the bridge infers
the agent from the `session:` source string (which is written as
`{AGENT_ID}-YYYY-MM-DD-HH-MM-SS` since v2.5.0).

- `formatChunks` now derives a tag from `c.source` when `c.agent_id` is
  absent.
- Patterns covered: `session:cc-2026-...`, `session:lmstudio-igor2-2026-...`,
  `session:dream-2026-04-25`. `mem0:*` chunks tag as `mem0`. Anything else
  still falls through as `?`.
- Known limitation: the server-side `agent_id` filter on `/context` is a
  ranking hint, not a strict filter — passing `agent_id=cc` to artforge can
  still return non-cc chunks if they rank highly. That's a Mnemo Cortex
  server issue; the bridge can't fix it. For exact-match retrieval, use
  unique marker phrases in your save content.

---

## v2.6.0 (2026-04-27)

Tools auto-detected based on available directories. Fresh installs see 9
core memory tools.

- **Default tool surface = 9 tools** (4 memory + 5 Passport).
- **Brain-lane + session tools (5)** auto-enable when `BRAIN_DIR` exists
  (default `~/github/sparks-brain-guy/brain`).
- **Wiki tools (3)** auto-enable when `WIKI_DIR` exists (default `~/wiki`).
- No flags, no env-var switches — if the directory is on disk, the tools
  register; if not, they don't.

| Setup | Tools registered |
|---|---|
| Default | **9** — memory + Passport |
| + brain dir | 14 |
| + wiki dir | 12 |
| + both | 17 |

---

## v2.5.0 — "One Bridge" (2026-04-26)

The MCP bridge becomes a single drop-in for every agent. Restores the
brain-lane, wiki, and auto-capture tools that came over from the archived
`mnemo-cortex-mcp` repo, layered on top of the existing Passport + share
bridge. One bridge. Every agent. No more "which server.js do I point at?"

### What Changed

- **Restored 8 tools that were removed in v2.3.0** when Claude Desktop's
  storage architecture forced the integration pull. They're back in the
  unified bridge: `opie_startup`, `read_brain_file`, `list_brain_files`,
  `write_brain_file`, `session_end`, `wiki_search`, `wiki_read`, `wiki_index`.
  The Passport + `mnemo_share` tools introduced in v2.4.x are unchanged.
  Total: 17 tools.
- **Auto-capture + nudge system ported.** Tool calls are buffered (8 entries
  or 2 min idle) and flushed to Mnemo as background activity trail. After
  20 calls without a manual save, responses get a save reminder appended.
  `SIGTERM` / `SIGINT` drain the buffer before exit. Passport tools are
  marked `skip` — their audit lives in passport's git log already.
- **Session ID prefix honors `MNEMO_AGENT_ID`.** CC's saves get `cc-`
  prefix, Rocky's get `rocky-`, Opie's stay `opie-`. The previous bridge
  hardcoded `opie-` regardless of the env var. (The fix already existed
  in the openclaw-mcp bridge from v2.4 — it's now propagated through the
  ported tools too.)
- **`session_end` git commit message respects `AGENT_ID`.** Was hardcoded
  `"brain: Opie session end"`; now uses the running agent's id.
- **Single shared HTTP client.** All tools route through the `mnemoRequest`
  helper with 10s timeout + abort-on-stall. Replaces the legacy bridge's
  fire-and-hope `fetch` calls.
- **`BRAIN_DIR`, `WIKI_DIR`, `DREAM_DIR` env vars** for non-default install
  paths. Defaults still match the Sparks-Brain reference layout — but
  non-Sparks users can override.
- **Archived `mnemo-cortex-mcp` repo stays archived.** No further changes
  there. The legacy `server.js` was patched in-place earlier the same day
  for the agent-ID prefix bug — fix kept locally for any installs still
  pointing at that path; this release supersedes it.

### Migration

If you're still pointing at the legacy bridge (`mnemo-cortex-mcp/server.js`)
or the slim openclaw-mcp bridge (`mnemo-cortex/integrations/openclaw-mcp/server.js`
pre-v2.5.0):

1. Pull latest `mnemo-cortex`.
2. `cd integrations/openclaw-mcp && npm install`.
3. Update your MCP config command path to point at this `server.js`.
4. Restart whatever spawns the bridge (Claude Code session, OpenClaw gateway).

Tool names and behavior are unchanged — existing prompts and agent muscle
memory keep working.

---

## v2.4.1 — "Developer's Passport" (2026-04-22)

Passport gets an honest name and the tuning loop lands its first real pass.
This is a dev-targeted release: the product is aimed at developers building
agent systems who want a known-good pattern for safe behavioral-claim
ingestion. The possessive in the name is deliberate — it drops when the
hosted / browser-AI story is ready for normal users. Not today.

### What Changed

- **Rebrand: Passport → Developer's Passport** (product name only; code
  paths, tool names, YAML schema, and REST API all unchanged). `passport/`
  stays `passport/`. `passport_get_user_context` still `passport_get_user_context`.
- **Policy tweaks applied after corpus run.** Three policy changes approved
  and committed against the shipped 200-entry eval corpus:
  - `bucket_defaults.semi_trusted_remote`: `review_required` → `allow`
  - `bucket_defaults.untrusted_web`: `local_only` → `review_required`
  - `dispositions.insufficient_evidence`: new key, `review_required`
  `validation.py` now routes the <2-evidence short-circuit through the
  policy map instead of hard-coding `hard_block`, matching the pattern used
  for every other disposition.
- **Eval numbers published.** Overall moved from 48.0% / 0.428 macro-F1
  (baseline) to 53.0% / 0.458 (+5pp / +0.030) after the tweaks. Per-class
  F1: `allow` +0.251, `review_required` +0.089, `hard_block` +0.027,
  `local_only` -0.246. The `local_only` regression is inherent to raising
  the `untrusted_web` floor — those cases now land at `review_required`
  where a human can make the call. Detail in `passport/README.md`.
- **README rewritten for developers.** UNDER CONSTRUCTION banner removed.
  Accurate 5-tool table. 5-minute dev quickstart. Honest Known Gaps section
  (no Phase 2 classifier, no hosted HTTP MCP wrapper, no review UI, weak
  `local_only` F1, no per-user repo sync automation).
- **Chrome extension and claude.ai HTTP connector work parked** — neither
  was shipping in this release and neither was honest to advertise. When
  there's a live user for the browser path, that work resumes. Until then,
  the dev integration via stdio MCP (`integrations/openclaw-mcp/`) is the
  shipped path.
- **Eval corpus held separately.** The 200-entry labeled corpus used to
  produce the numbers above contains detector-bait tokens (fake-but-
  well-formed API keys) that trip public secret scanners. The harness
  (`tests/passport/corpus_score.py`) is in the repo; the corpus itself
  ships on request — open an issue for access.

### Why This Matters

The previous framing — "portable AI identity that travels with you to any
AI" — was writing a check the shipped code couldn't cash. The stdio MCP
integration works. The HTTP-to-claude.ai path doesn't exist yet. Renaming
to *Developer's* Passport aligns the pitch with what the product actually
delivers: a reference-grade safety + review-queue layer for devs who want
to wire it into their own agent stacks today.

### Models / Cost

No model changes. Validator is deterministic rule + detector logic; no LLM
calls in the hot path. Eval harness calls no LLM — it scores the current
validator against the labeled corpus.

---

## v2.4.0 — "Compile, Connect, Adapt" (2026-04-22)

The biggest release since v2.0. Three new feature surfaces land alongside the existing memory + dreaming core. Mnemo is now a full memory architecture, not just a memory store.

### What Changed

- **WikAI compiler** — `mnemo-wiki-compile.py` lands in the repo. Nightly cron at 3:30 AM (15 min after Dreaming) reads recent Mnemo memories, clusters by topic in Python (no LLM routing), then per-topic calls gemini-2.5-flash to rewrite the corresponding wiki page integrating new information. Cross-references are validated against the live page set — no hallucinated wikilinks. Every page carries a provenance footer listing source memory session IDs. Per-page failure isolation; one bad LLM call posts ⚠️ to `#alerts` and the run continues. Cost: ~$0.01–$0.05 per nightly. The wiki is never edited directly; Mnemo is the source of truth.
- **Sparks Bus** — agent-to-agent messaging with delivery confirmation, shipped as `sparks_bus/` inside this repo and standalone at github.com/GuyMannDude/sparks-bus. Doctrine: Discord = doorbell, Mnemo = mailbox, tracking ID = receipt. Lifecycle in `#dispatch`: 📬 DELIVERED → ✅ PICKED UP → 🔄 LOOP CLOSED. One-shot ⚠️ alerts on failure (no retry storms). Two install modes auto-detected: Full (with Mnemo) or Standalone (payload in Discord notification). A2A-compatible: Agent Cards in `sparks_bus/agent-cards/`, task-shape translator in the watcher, `A2A.md` mapping reference, `SETUP-PROMPT.md` for AI-bootstrapped deployments.
- **Passport** — portable user working-style preferences. Five MCP tools (`passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override`, `passport_get_user_context`). Observations become candidates, only stable claims promote — nothing auto-lands in the user's profile.
- **Three-layer architecture documented** — Mnemo (source of truth, query-time) + WikAI (compiled view, write-time) + Brain files (live state, ephemeral). When they disagree, Mnemo wins.
- **Inspirations credited openly** — Karpathy's LLM Wiki pattern (WikAI), Nate B Jones's hybrid analysis (three-layer architecture), Google A2A (Sparks Bus compatibility), Mem0 (bridge not replace).
- **README + CAPABILITIES + landing page updated** — feature overview block, new sections, updated full-stack ASCII diagram.

### Why This Matters

Before v2.4, agents could remember (Mnemo), agents could share (Dreaming), and agents could fall back to depth (Mem0). After v2.4, agents can also:
- Read a *compiled* understanding of the project state without re-deriving from raw data on every query (WikAI)
- Send each other tracked, ack'd messages with the lifecycle visible to the operator in one Discord channel (Sparks Bus)
- Adapt their tone and workflow to how *this user* works (Passport)

The v2.4 release is when Mnemo became a memory architecture, not a memory server.

### Models / Cost

No model changes. WikAI compiler + Dreaming both run on `google/gemini-2.5-flash` via OpenRouter. Combined nightly cost: under $0.10.

---

## v2.3.2 — "Fresh Models" (2026-04-11)

Doc audit triggered by external user report: setup guide referenced dead Google model name (`text-embedding-004`, shut down Jan 2026), causing hours of debugging silent failures.

### What Changed

- **Model tier table updated** — Added Google cloud tier (`gemini-embedding-001` + `gemini-2.5-flash`), updated OpenAI reasoning model to `gpt-4.1-nano` (10x cheaper than `gpt-4o-mini`). All model names verified against current provider APIs.
- **Google deprecation warning** — Explicit callout that `text-embedding-004` is dead, use `gemini-embedding-001`.
- **Troubleshooting section added to README** — Covers the three most common failure modes: "No chunks" (wrong embedding model name), compaction model unreachable, server unreachable. Includes current model name table by provider.
- **Expected test output added to README** — Users can now see what a passing smoke test looks like before they run it.
- **Version bumped** — pyproject.toml synced to 2.3.2.

### Problem This Solves

Model names change without notice. A user following our docs could configure a dead model, get zero results from recall, and have no idea why. The troubleshooting section now explicitly warns about this and lists current model names by provider.

### Models Verified (April 2026)

| Provider | Embedding | Reasoning | Status |
|----------|-----------|-----------|--------|
| Ollama | nomic-embed-text | qwen2.5:32b-instruct | Current |
| OpenAI | text-embedding-3-small | gpt-4.1-nano | Current |
| Google | gemini-embedding-001 | gemini-2.5-flash | Current (flash sunsets June 2026) |

---

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
- **Sidecar architecture** — Version-resistant design that observes session files from outside the agent. Mnemo keeps your memory on disk — if either process restarts, the data is already there.

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
