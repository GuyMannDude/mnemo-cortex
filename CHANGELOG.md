# Changelog

## v2.11.0 (2026-05-16) — sqlite-vec vector index (Mnemo v4 Phase 2)

Before this release, vector recall was a linear scan. Every `/context`
call read every memory bundle from disk, computed cosine similarity in
Python, and ranked the result. At 1,398 production memories the L1/L2/L3
chain works; at 14,000 it doesn't. The index needed to live next to the
memories, not be rebuilt from JSON on every read.

This release adds an indexed sqlite-vec table per agent. The existing
L1 (project precache) and L2/L3 (linear scans) tiers stay — the new VEC
tier slots between them and answers semantic recall in milliseconds even
on the full corpus.

**What's new in `agentb/`.**

- **`agentb/vec.py`** — new module. `VecStore` wraps a per-tenant SQLite
  file with two tables: `vec_sources(memory_id, text, source_file, created_at)`
  and `vec_embeddings USING vec0(memory_id, embedding FLOAT[768])`. Source
  text and vectors are stored separately so the embedding index is
  rebuildable without touching memory bundles (Open Brain's
  source/vector separation principle). Dimension is locked to 768 —
  matched to `nomic-embed-text`, the default primary embedder. A vector
  of the wrong dimension raises `VecDimMismatch` and refuses the write
  rather than silently dropping data; loud failure beats silent vector
  loss (Vapor Truth doctrine).
- **`agentb/vec.py`** — auto-detected operating modes. `detect_mode(memory_dir)`
  returns `migration` when JSON entries already live on disk and `clean`
  otherwise. Existing installs upgrade in place: their bundles stay as
  the source of truth and a one-shot backfill populates the vec index.
  Fresh installs initialize empty. The user never picks a mode.
- **`agentb/vec.py`** — `backfill(store, memory_dir, embed)` walks the
  memory directory, re-embeds canonical `summary + key_facts` text per
  bundle, and upserts into the vec index. Idempotent (skips already-indexed
  ids) and tolerant (continues past per-entry embedding failures and
  malformed files). Returns counters for `total / embedded / skipped /
  failed / elapsed_sec`.
- **`agentb/server.py`** — `TenantManager` now constructs a `VecStore` at
  `<data_dir>/vec_index.sqlite` for every agent and records the detected
  mode. `/writeback` reuses the embedding it already computed for the L2
  tier and upserts it into the vec index in the same transaction. The
  `/context` pipeline gains a VEC tier between L1 and L2 — when the vec
  index has data, the query embedding runs against vec0's k-NN, hits are
  wrapped as `ContextChunk(cache_tier="VEC")`, and `cache_hits["VEC"]`
  surfaces in the response. L2/L3 still run when VEC doesn't return
  enough chunks; nothing else in the chain changed.
- **`agentb/server.py`** — `GET /vec/status?agent_id=X` reports mode,
  indexed count, on-disk memory count, and db path for the tenant.
  `POST /vec/backfill?agent_id=X` runs the backfill walk on demand and
  returns the stats dict. Use this once on upgrade for existing installs;
  dreaming can call it nightly for ongoing maintenance.
- **`pyproject.toml`** — adds `sqlite-vec>=0.1.6` as a runtime dependency.
- **`tests/test_vec.py`** — new test module. Sixteen tests cover store
  init, upsert/replace/delete, dimension-guard rejection (write and
  query paths), mode detection across empty / json-present / missing
  directories, `iter_memory_entries` (canonical text shape, empties
  skipped, corrupt files tolerated), backfill (single-pass, idempotent,
  failure-tolerant), and the spec's canonical semantic-over-keyword
  scenario.

**Verified on the production corpus.** Backfilled 1,398 cc-agent memory
bundles in 86.5s (~62 ms/entry, Ollama-bound) and observed vec0 k-NN
queries returning top-3 in 3–4 ms over the full set. The same query
shape against the legacy linear-scan path touches every bundle on every
call.

**Upgrade path.** `pip install --upgrade mnemo-cortex`, restart, then
`POST /vec/backfill?agent_id=<your-agent>` once. The vec index appears
alongside `memory/` inside each agent data directory. Memory bundles
stay where they are. Nothing else changes for the user.


## v2.10.0 (2026-05-16) — Provenance & decay land in the open-source core

The MCP bridge announced provenance & decay support back in v2.8.0, but
the Python core that stores memories never received the matching code.
Internal deployments ran a hand-maintained fork while the published
package shipped a pre-provenance backend. Bridge sent the fields, the
backend ignored them.

This release closes that gap. One source of truth.

**What's new in `agentb/`.**

- **`agentb/provenance.py`** — new module. `VALID_SOURCES`,
  `VALID_CATEGORIES`, `DECAY_THRESHOLDS`, `DEFAULT_HIDDEN_CATEGORIES`, the
  regex `PROVENANCE_PATTERNS`, `suggest_category(text)`, and
  `compute_stale_warning(category, created_at)`.
- **`agentb/server.py`** — `WritebackRequest` adopts `source`, `category`,
  `additional_tags`. `WritebackResponse` returns `category_used`,
  `category_suggested`, `category_match_keywords`, `source_used`.
  `ContextRequest` adopts `source`, `category`, `exclude_categories`,
  `exclude_stale`, `max_age_days` filters. `ContextChunkResponse` surfaces
  `provenance_source`, `category`, `additional_tags`, `age_days`,
  `stale_warning`. Writeback runs the regex auto-suggester when no
  category is given and persists `schema_version: 3` on every record.
  `keep_chunk()` applies the filter set across HOT/L1/L2/L3 with 3× over-
  fetch so post-filter trims don't leave callers short. `session_log` is
  hidden by default — pass `exclude_categories=[]` to disable.
- **`agentb/cache.py`** — `ContextChunk` gains optional v3 fields. `L2`
  search reads `metadata.provenance_source` / `metadata.category` /
  `metadata.additional_tags` and computes `age_days` + `stale_warning` per
  chunk. `l3_scan()` does the same straight off the memory_entry record.
- **`agentb/config.py`** — `AgentConfig` adds `mem0_user_id` and
  `mem0_fallback_only` overrides. New `resolve_mem0(cfg, agent_id)` helper
  routes per-agent Mem0 traffic. Two common shapes — agents sharing a
  Mem0 user namespace, or each agent owning its own — both expressible
  in `agentb.yaml`:

  ```yaml
  agents:
    primary-agent:
      mem0_user_id: primary
      mem0_fallback_only: false   # always query Mem0 alongside local
    secondary-agent:
      mem0_user_id: shared        # writes/reads against the same Mem0 user
      mem0_fallback_only: true    # only hit Mem0 when local misses
    tertiary-agent:
      mem0_user_id: shared
      mem0_fallback_only: true
  ```

**Backward compatibility.** Old records (no v3 fields on disk) load
without issue — `keep_chunk` treats unset fields as "pass" when no filter
is active. Old callers that don't pass v3 params on writeback get
`source: inferred` + the auto-suggested category. The on-disk field name
`provenance_source` in L2 metadata stays as it is — no migration script
needed. Strict source filter (`source=user`) drops pre-v3 chunks on
purpose: they have no provenance to evaluate.

**Decay thresholds (days).** Topology warns at 30, stale at 90.
current_state and unknown warn at 90. Relationship warns at 180.
session_log warns at 90. Doctrine, incident, identity, decision are
perpetual — never stale. Override via env: `MNEMO_DECAY_TOPOLOGY_WARN_DAYS`,
`MNEMO_DECAY_RELATIONSHIP_WARN_DAYS`, etc.

**FastAPI app version** bumps to `0.7.0` (`agentb/server.py`); package
version (`pyproject.toml`) bumps to `2.10.0` to match.

**`mnemo_v2/` retired.** The repo previously carried a parallel
conversation-archive product under `mnemo_v2/` — SQLite + FTS5 over
message transcripts with leaf/condensed summary compaction. It never
shared callers with the memory engine in `agentb/`: nothing imported
from it, `install.sh` didn't deploy it, the MCP bridge didn't route to
it, and `tests/test_smoke.py` was its only consumer. Two parallel
servers under one repo confused readers about which path was "the"
backend. Removed (`mnemo_v2/`, `tests/test_smoke.py`, the deprecated
`integrations/claude-code/mnemo-watcher-cc.sh` shim that targeted it,
plus the `pyproject.toml` and `scripts/wheel-smoke-test.sh` references).
If conversation compaction is wanted in the future, building it on
`agentb/` directly is simpler than maintaining a parallel server.

---

## v2.9.0 (2026-05-15) — Developer Dump (Mnemo v4 Phase 1)

First piece of the Mnemo v4 roadmap. Adds bridge-level JSONL capture of
every MCP tool call across all agents that route through the shared MCP
bridge (CC, Opie, Rocky, Hermes, Claude Desktop, anything else). Default
**off** — no surprise data collection for public users. Flip on with one
env var.

**Problem this solves.** When a tool call silently fails (Peter Widget
went dark for hours and nobody noticed), there's no trace to look at. The
bridge knew every detail of what happened — tool name, params, response,
latency, success/failure — and threw it all away. Same loss when you want
training data, a debugging trail, or to understand why an agent
flip-flopped three times on the same answer.

**What ships.** A new `dump.js` module in the MCP bridge that wraps every
tool handler with a writer. One monkey-patch on `server.registerTool`
covers all 18 existing tools and any future additions. Output is JSONL,
one file per agent per day, human-greppable with `jq`:

```bash
# Turn it on for your bridge
export MNEMO_DUMP=on
# (optional) custom dir, default ~/.mnemo-cortex/dumps
export MNEMO_DUMP_DIR=~/dumps
```

```jsonl
{"ts":"2026-05-15T00:00:00.000Z","kind":"header","schema_version":1,"mnemo_version":"2.9.0","agent_id":"rocky"}
{"schema_version":1,"ts":"2026-05-15T20:14:33.123Z","kind":"tool_call","agent_id":"rocky","tool":"mnemo_save","params":{...},"response":{...},"latency_ms":47,"ok":true}
{"schema_version":1,"ts":"...","kind":"tool_call","agent_id":"rocky","tool":"mnemo_recall","params":{...},"response":null,"latency_ms":12,"ok":false,"error":"ECONNREFUSED"}
```

**Failure capture.** Handlers in the bridge catch internally and return
`{isError: true}` instead of throwing — the dump still records those as
`ok: false` with the error message extracted from the response text. Real
thrown errors are also captured and then re-thrown unchanged.

**Fail-loud on write errors.** If the dump directory becomes unwritable
(disk full, permission flip), the writer logs `[MNEMO DUMP FAIL]` to
stderr once per failure streak. Tool dispatch keeps working — the dump
never breaks the bridge.

**CLI for inspection** (server-side, via `mnemo-cortex`):

```bash
mnemo-cortex dump list                  # all dump files, size + line count
mnemo-cortex dump tail rocky            # live-tail today's rocky dump
mnemo-cortex dump tail rocky --no-follow # one-shot print
```

**Zero overhead when off.** `dump.wrap()` returns the original handler
unchanged when `MNEMO_DUMP=off` (the default). No allocation, no closure,
no measurable cost.

**Schema versioning.** Every line carries `schema_version: 1`. Future
Phase 1.5+ additions (per-agent message capture, custom content filters)
will bump the schema cleanly.

**Tests:** 10 new tests in `dump.test.js`, all passing — covers off-mode
no-op, on-mode header+event, two-agent isolation, day rollover, write-
failure handling, success capture, isError capture, thrown-error capture,
disabled passthrough, and `listDumps()`. Runs without a Mnemo server.

**Scope discipline.** Phase 1 only captures what the bridge sees — MCP
tool traffic. Agent-side messages (user prompts, agent text responses,
raw Claude API exchanges) live in agent processes and need per-agent
hooks. That's the Phase 1.5 frontier. See
`brain/mnemo-v4-phase1-dump-spec.md` in `sparks-brain-guy` for the design
rationale; bus #223 for the pressure-tests that produced it.

Version alignment: this release also bumps `pyproject.toml` (was 2.7.1)
and the CLI `--version` string (was 2.6.4) to match the CHANGELOG /
bridge versions. They had drifted across the last few releases.

---

## v2.8.2 (2026-05-15) — Installer hardening (non-interactive + DRY_RUN fixes)

Two installers had bugs that silently broke `curl | bash` and CI workflows.
Found while auditing why a community user reported install trouble.

**Fix 1 — `integrations/hermes/install.sh` hung in non-interactive mode.**

Hermes Agent's `hermes mcp add` (v0.12.0+) prompts *after* tool discovery
with "Enable all N tools? [Y/n/select]". The installer didn't pipe a
response, so any non-TTY run (curl|bash, CI, dotfile bootstrap, restricted
shell) hung there forever — or, on some shells, silently dropped the
config persist step. The user saw "✓ Connected!" and then nothing.

Now: the installer detects `[ -t 0 ]` at startup. In non-interactive mode
it pipes `yes Y` into `hermes mcp add` to auto-accept tool selection,
and honors env vars in place of the interactive prompts:

```bash
MNEMO_URL=http://localhost:50001 \
MNEMO_AGENT_ID=hermes \
MNEMO_SHARE=separate \
MNEMO_REPLACE=1 \
bash install.sh < /dev/null
```

Replace-existing requires explicit `MNEMO_REPLACE=1` in non-interactive
mode rather than silently overwriting a working config. Unreachable
Mnemo aborts loudly instead of wiring Hermes to a dead server.

**Fix 2 — `robot-install.sh` DRY_RUN leaked API keys to disk.**

`MNEMO_INSTALL_DRY_RUN=1` was meant to skip side effects, but step 3
(config + env file + data dirs) was never gated. A user testing the
installer with their real `OPENROUTER_API_KEY` set in the environment
would silently have that key written into
`~/.config/mnemo-cortex/mnemo-cortex.env` (mode 0600). Dry-run with a
real key was a foot-cannon.

Now: step 3 is wrapped in the same `DRY_RUN` guard as steps 2/4/5.
Dry-run reports the paths each step *would* write but produces zero side
effects on disk. Verified with `OPENROUTER_API_KEY=test-not-real
MNEMO_INSTALL_DRY_RUN=1 ./robot-install.sh` — no files created.

**Doc fixes alongside.** README clarifies that `robot.install` sets up
the **server only**; agent integrations live under `integrations/`.
Hermes integration README updated for the current 18-tool surface
(was 17 — `agent_startup` got added) and notes that the config entry
lands at `~/.hermes/profiles/<profile>/config.yaml` for profile-using
Hermes setups, not just `~/.hermes/config.yaml`.

## v2.8.1 (2026-05-13) — MCP bridge directory rename

`integrations/openclaw-mcp/` → `integrations/mcp-bridge/`. The bridge
code at the old path was never OpenClaw-specific — it's the generic
Node.js MCP server that every Mnemo Cortex integration (Claude Desktop,
Claude Code, OpenClaw, LM Studio, AnythingLLM, Agent Zero, Hermes Agent,
Ollama Desktop, etc.) spawns on stdio. The old directory name misled
new users; the new path tells the truth.

**Rename-only release. No functional change.** The 8 host-specific
integration directories all had their install scripts + config
examples + READMEs updated to point at the new path. Server.js +
package.json + CHANGELOG + tests moved with `git mv`.

**Back-compat:** the old path `integrations/openclaw-mcp/` is kept as
a thin stub — symlinks at `server.js` and `package.json` resolve to
the new location, plus a README explaining the move. Existing MCP
client configs pointing at the old path keep working without action;
update them at your convenience. Will be removed in a future major
version.

Full bridge release notes: [integrations/mcp-bridge/CHANGELOG.md](integrations/mcp-bridge/CHANGELOG.md)

## v2.7.1 (2026-05-04)

Public-release scrub. Mnemo Cortex was developed inside Project Sparks
against a specific multi-agent setup (CC, Rocky, Opie, BW, Cliff, Sparky,
Alice). The source carried that history in defaults, examples, system
prompts, and test fixtures. This release strips the customer/operator/agent
specifics so a fresh user gets a stock/default toolbox they can configure
for their own stack.

### Bridge (integrations/openclaw-mcp, 2.7.0 → 2.7.1)

- **`opie_startup` alias neutralized.** Was hardcoded with a multi-paragraph
  Opie-flavored identity prompt naming a specific operator, machine, and
  team. Now returns a minimal "you ran the deprecated alias, your identity
  lives in opie.md" header. Identity belongs in the brain lane file, not
  in bridge code. The alias still loads opie.md and forces agent_id=opie
  for back-compat — only the static identity block changed.
- **Tool descriptions made generic.** "Sparks Brain directory" → "brain
  directory ($BRAIN_DIR)". `write_brain_file` description no longer names
  specific lane files.

### Synthesis scripts

- **`mnemo-dream.py`**: agent list is now auto-discovered from
  `~/.agentb/memory/<agent>/` subdirectories at runtime. Override with
  `MNEMO_DREAM_AGENTS` env var (comma-separated). System prompt rewritten
  to be agent-agnostic — describes "a multi-agent workspace" without
  naming specific agents or their roles. The agents declare their roles
  through the memories themselves; the synthesizer reads them.
- **`mnemo-wiki-compile.py`**: agent aliases now load from
  `MNEMO_WIKI_AGENT_ALIASES` env var (JSON). Was a hardcoded map of
  Sparks-internal agent names. Discord token / channels paths default to
  `~/.mnemo-cortex/` (was `~/.sparks/`).

### Docs / examples

- **THE-LANE-PROTOCOL.md** task-shape examples now use generic slugs
  (`auth-rate-limit`, `builder`, `architect`) instead of project-specific
  ones (`hoffman-gmc-appeal`, named-agent assignees).
- **`integrations/hermes/README.md`** cross-agent example uses a generic
  "build agent" / "deploy issue" instead of a specific customer/incident.
- **`integrations/openclaw-mcp/README.md`** `MNEMO_AGENT_ID` examples use
  role-shaped names (`assistant`, `builder`, `researcher`).
- **`agentb/recall/parser.py`** docstring example bullets now use
  `@user` / `@builder` instead of named operator/agent.
- **README.md** Origin Story tightened — narrative pointer to
  `FINDING-MNEMO.md` for the full backstory; Credits section keeps the
  contributor list as project history but drops internal-infra
  references.
- **`tests/ongoing/daily-feed.sh`** synthetic test data fully rewritten —
  fictional company, generic agent roles, no real names, locations, or
  customers. The test-questions schema is preserved; only the content
  changed. Stale `tests/ongoing/test-questions.json` removed (the
  generator regenerates it on next run).

### Sparks Bus example agent cards

- Old cards (`bw.json`, `cc.json`, `cliff.json`, `opie.json`, `rocky.json`)
  named specific Sparks-internal agents and infrastructure (Tailscale
  hostnames, internal Discord channels). Replaced with three generic
  cards (`researcher.json`, `builder.json`, `architect.json`) — one per
  delivery method (Discord channel, subprocess, queue/pull). Same A2A
  shape, same Sparks-Bus delivery block format, no real identifiers.
- `sparks_bus/A2A.md` table updated to match.

### Minor

- All `HTTP-Referer` headers in OpenRouter calls now point at the public
  GitHub repo URL instead of the maintainer's personal site.
- `passport/config.py` skeleton example uses `GreenLeaf` (fictional)
  instead of `Hoffman Bedding` (real customer name from origin context).

## v2.6.5 (2026-05-01)

Two install-blocking fixes found while wiring Hermes Agent against the public
package. Both were silent in 2.6.4 — the package installed, but
`mnemo-cortex start` crashed at import time, and even if it had started, the
`--port` flag was dead code.

- **ImportError on startup fixed.** `agentb/server.py` imported `Mem0Config`
  from `agentb.config`, but the class was never defined there and
  `agentb/mem0_bridge.py` was never committed. Result: any fresh
  `pip install mnemo-cortex && mnemo-cortex start` died with
  `ImportError: cannot import name 'Mem0Config' from 'agentb.config'`. The
  Mem0 dataclass + AgentBConfig field + YAML loader + `Mem0Bridge` class are
  now present in the repo. Mem0 is opt-in (`enabled: false` by default), so
  users who don't configure Mem0 simply never trigger the upstream code path.
- **`--port` flag now overrides config.** `mnemo-cortex start --port 50002`
  previously captured the value but never passed it to the server subprocess
  — the bind port came exclusively from `agentb.yaml`'s `server.port`. The
  CLI now exports `MNEMO_PORT` to the subprocess env, and `agentb/server.py`
  honors it as an override over `cfg.server.port`. The yaml stays
  authoritative when the flag is omitted.

No 2.6.4 CHANGELOG entry exists; it shipped as a version bump without a
release note. Anything that landed between 2.6.3 and 2.6.5 is in `git log`.

## v2.6.3 (2026-04-27)

Audit-driven catalog polish (Opie's compliance pass). Two real fixes plus
one nice-to-have, all narrowed from a line-by-line check of the v2.6.2
bundle against the MCPB spec + Connectors Directory submission requirements.

- **`tools_generated: true`** declared in the manifest. The bridge
  conditionally registers up to 8 additional tools at startup based on
  `BRAIN_DIR` / `WIKI_DIR` presence, so the manifest tool list isn't always
  exhaustive. This flag tells reviewers and hosts that's intentional.
- **`tools` array expanded to all 17** with explicit "Conditional (BRAIN_DIR)"
  / "Conditional (WIKI_DIR)" prefixes on the optional ones. Previously the
  manifest declared 9; a reviewer testing on a Sparks-style box would have
  seen 17 and flagged the mismatch.
- **`icons` array** added alongside the existing `icon` string for richer
  catalog rendering. Single 512×512 entry today; room for theme variants
  later without further schema changes.
- Bundle rebuilt; size unchanged (~3.6 MB packed).

No code changes in this release — manifest only, plus the version bump.

---

## v2.6.2 (2026-04-27)

Catalog-grade polish on the Claude Desktop bundle.

- All 17 tool registrations migrated from the deprecated `server.tool(...)`
  signature to `server.registerTool(...)` with proper `annotations`. Every
  tool now declares `title` plus `readOnlyHint` / `destructiveHint` /
  `idempotentHint` / `openWorldHint` per the [MCP tool annotations spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#tool-annotations).
  Hosts can now show a clear destructive-action warning before
  `write_brain_file`, `session_end`, and `passport_forget_or_override` fire.
- `manifest.json` declares `privacy_policies` pointing at `PRIVACY.md`.
- New top-level `PRIVACY.md` documents the data flow plainly: bundle talks
  only to the user-configured Mnemo Cortex URL, no telemetry, no third-party
  services unless the user's server is configured that way.
- Claude Desktop README adds a Privacy section.
- Bundle rebuilt with the updated bridge.

These changes meet the requirements for submission to Anthropic's curated
Connectors Directory ([docs](https://claude.com/docs/connectors/building/submission)).

---

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
