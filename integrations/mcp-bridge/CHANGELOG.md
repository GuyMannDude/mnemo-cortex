# Changelog

> **Note on version history:** The bridge used to track the main
> `mnemo-cortex` package version step-for-step. That coupling loosened
> once the main package added features the bridge didn't need to
> change for — Phase 3 Facts wired through as a thin passthrough,
> the Mem0 retirement was server-side only. (The current bridge version
> lives in `package.json` — a hardcoded number here went stale.) Versions between 2.0.1 and 2.6.4 shipped
> server-side and tooling changes (Dreaming, WikAI, Sparks Bus,
> Developer's Passport, new host integrations) that didn't materially
> change bridge behavior — the bridge continued to work unchanged
> through those releases. The full history is in the main repo
> [CHANGELOG.md](../../CHANGELOG.md).

## 2.23.1 — 2026-08-20 — boot budget rebalance: 500 more active.md → doctrines.md

**Problem.** The doctrines.md boot index hit FULL at 35 doctrines — 321 spare
against the 325 margin floor — so the next doctrine ANY agent wrote was
gate-blocked (first casualty: Opie's security-spending doctrine, Guy's 08-20
ruling, which could only fit by degrading eight other claim lines for 44
chars). Meanwhile active.md idled at 5,435 of 9,000 behind its NO-GROWTH gate.

**Fix.** `STARTUP_BUDGETS`: active.md 9,000 → 8,500; doctrines.md 6,500 →
7,000. Net zero — the sum stays 38,500, no host sees a larger block (same
shape as the S220 move, one wall later). Guy approved 2026-08-20. #2391's
borrowed-against-a-gate caveat now covers the full 1,500: relax
board-check.py's no-growth gate and this must be restored or re-measured.

## 2.23.0 — 2026-08-19 — every tool call now leaves a latency record

**Problem.** `mnemo_save`/`mnemo_recall` logged no timing, so a hang that
cleared between wedge-watch samples left zero record — the watch samples
state, not intervals (snag-mnemo-verbs-no-latency-record; the 08-07
incident's only trace was bus pings). The Developer Dump does record
latency, but it captures full params and responses and is default OFF —
too heavy to be the always-on answer.

**Fix.** New `latency.js`: a slim, always-on wrapper around every tool
handler writes one JSONL line per call — `tool`, `ms`, `ok` — to
`~/.mnemo-cortex/latency/<agent>/<date>.jsonl` (UTC-dated, one file per
day, reusing the dump module's daily file naming and fail-loud-once
handling — note neither module prunes old files; lines here are tiny). Measured bridge-side, so it
includes the network time the server cannot see; a handler returning
`isError` or throwing records `ok:false`. No params, no response bodies.
Opt out with `MNEMO_LATENCY=off`; relocate with `MNEMO_LATENCY_DIR`.

## 2.22.0 — 2026-08-19 — session_end stops sweeping other agents' work into its commit

**Problem.** `session_end` staged with `git add -A` in the one repo five
agents share. A dirty tree is the NORMAL mid-session state, so whoever
ended a session first swept every other agent's in-progress edits into a
commit under its own name — misattributing work, landing unfinished files
with no gates run, indistinguishable from a correct commit afterward. The
`git-add-specific-paths` doctrine is hook-enforced on agents' shells, but
the bridge shells out from node and never passes through the hook
(snag-session-end-git-add-all). It hadn't bitten only because
`write_brain_file` auto-commits keep the tree nearly clean — timing, not
safety. Second, the exit line "Total tool calls this session" counts only
capture-instrumented tools, so it under-reported every session
(snag-session-end-tool-call-counter).

**Fix.** New `sessionEndCommit()` in `brain-git.js` (tested): stages ONLY
the ending agent's own files UNDER the brain dir (basename `<agent>.*` /
`<agent>-*` — lane, session archives, archive index) via `:/` root
pathspecs, names exactly what it committed in the report line, and REPORTS
anything left dirty instead of sweeping it ("Left uncommitted (not yours
to sweep): ..."). Status parsing is NUL-terminated + unquoted
(`core.quotePath=false`, `-z`, `--untracked-files=all`) so renames commit
both sides, non-ASCII names don't poison the batch, and untracked
directories expand to stageable files. If
a leftover is the agent's own shared-file edit, the line says to re-save
it via `write_brain_file`, which commits pathspec-scoped. The counter line
is renamed to what it measures: "Captured tool calls this session
(activity-trail subset)". Also: `test.js`'s write test now accepts the
server's v4.15.0 near-dup HELD response — the identical-every-run test
payload trips the dedup filter, which still proves the write path.

## 2.21.0 — 2026-08-12 — the write that creates an overrun now says so, instead of the boot that suffers it

**Problem.** `agent_startup` caps every boot-loaded file and keeps the head —
the tail is dropped. Nothing measured a file at the moment it was **written**,
so an agent learned it had overrun only at the *next* boot, from a manifest
inside the payload, a session too late to remember what the dropped tail said.
It failed **3 of 9 days** (2026-08-03/04/11); the 08-11 specimen cut `opie.md`
**976 units** past the cap, and nothing said so until the following morning.
The gate existed the whole time — `lane-check.py` — but it lived at commit
time, on a different machine's habit, and it is not what agents call.

**Fix.** New `write-budget.js`, wired into two places:

- **`write_brain_file`** — assesses the content it just wrote against the
  slice that will cap it, and appends the overrun to the tool result.
- **`session_end`** — re-reads the agent's lane **from disk** and reports the
  same, catching lanes edited by a plain file tool that never passed through
  the bridge at all.

⚠️ **It never blocks a write.** A refused lane write at session end loses the
update outright, which is strictly worse than an oversized lane: the tail is
dropped at *boot*, not on disk, and `read_brain_file` still returns the file
whole. Write, then scream (`doctrine-degrade-to-raw`).

Silent unless the file is boot-loaded **and** at risk — snags, doctrine bodies
and archives are the overwhelming majority of brain writes and must not warn,
or the guard trains its reader to skip it (`doctrine-wrong-guard`). A declared
`BOOT BOUNDARY` is honored as deliberate, not reported as a fault.

**Semantics are `lane-check.py`'s, deliberately** — same budget source, same
UTF-16 ruler, same boundary rule, same floor, and the same CUT-vs-LIES split
(a marker sitting *past* the real cut is named separately, because the owner of
a cut lane knows they have no boundary and the owner of a lying one believes
theirs). Verified by running both against all five live lanes: identical
totals, statuses, and Opie's 565-unit spare.

⚠️ **TIGHT / BOUND / LIES are lane-only.** Review caught this before it shipped:
the flat 500-unit floor is *lane* calibration (Opie #1986). The shared docs are
`boot-budget-check.py`'s, and it uses a **proportional** floor
(`max(100, budget // 20)`, Opie #2011) — so a flat 500 against `people.md`'s
2,000 budget is 25% of the whole file, and the guard would have screamed TIGHT
at `people.md` (278 spare) and `CLAUDE.md` (360 spare) from day one, on files
the authoritative gate calls healthy and that boot whole. **The first version of
this feature was the exact failure it cites `doctrine-wrong-guard` to avoid**,
and the lanes-only cross-check missed it because the divergence was in the files
the check never looked at. Shared docs now report only the verdict both tools
reach without arithmetic — over budget, or not — rather than re-homing the
proportional rule here and creating the second copy this module exists to avoid.
A `BOOT BOUNDARY` in a shared doc no longer excuses an overrun either; that is
lane semantics and blessing a 20K board with it would contradict a `[FAIL]`.

**`MARGIN_FLOOR` now has one home** (`boot-budget.js`), read by `write-budget.js`
(import) and `lane-check.py` (parsed via `boot-budget-check.py`). It had been a
`FLOOR = 500` literal in the Python — the same shape as the stale `BUDGET = 11_000`
caught the day before (`snag-lane-check-hardcoded-budget`). Proven live, not
assumed: setting the bridge constant to 600 flipped `lane-check.py` to TIGHT and
exit 1, and restoring it flipped it back.

The advisory sits in **its own** `try/catch` at both call sites. In
`write_brain_file` the file is on disk and committed by the time it runs, so a
throw inside the handler's outer `try` would have returned
`Error writing <file>` — telling an agent its lane update was lost when it had
landed, which is worse than the overrun being reported.

**Verified end-to-end**, not just in unit tests: the real server driven over
stdio against an isolated brain wrote a lane 976 units over, returned the
warning in the tool result, and left the file whole on disk (11,976 units); the
healthy control returned no warning at all. All four live shared docs and all
five live lanes now agree with their owning gate. 25 new tests; all 68 bridge
tests green.

⚠️ **INERT UNTIL EACH CLIENT RESTARTS** — every MCP bridge process on IGOR
predates this file. Nothing is guarded until they cycle.

## 2.20.3 — 2026-08-12 — doctrines get 1,000 units from the board's unusable slack

**Problem.** `doctrines.md` sat at 5,191/5,500 — 309 units spare — and Opie
**stopped authoring** rather than write into a full store, four doctrines queued
behind it (incl. agreement-is-not-corroboration, #2382). Meanwhile `active.md`
sat at 5,993/10,000 holding **4,007 units it has no route to spending**: the
board is NO-GROWTH gated by `board-check.py`, so that headroom is reserved for
growth that is deliberately forbidden. Idle budget behind a gate is not
prudence, it is a stalled colleague.

**Fix.** `active.md` 10,000 -> 9,000, `doctrines.md` 5,500 -> 6,500.

⚠️ **This is NOT 2.20.0 repeated.** That raised `BOOT_TARGET` on a limit measured
against one host and spent it across five; Opie's next session maxed out tool
use twice and it was reverted the same day. This changes the **split**, not the
**size**: budgets still sum to 40,500, `boot-budget.test.js` reports the same
worst-case boot of 44,800, and no host — measured or unmeasured — is handed a
larger block than it has been booting on all along. A reallocation cannot
regress a ceiling it does not move. Revert = swap the two numbers back.

**Also in this release: the version number itself was lying.** `package.json`
read **2.19.1** (last bumped 2026-08-04) while this CHANGELOG documented 2.20.0,
2.20.1 and 2.20.2, all shipped 08-11. Three releases changed code and changelog
and never touched the version field, so anything reporting the bridge version
reported a build four releases stale. Bumped 2.19.1 -> 2.20.3 here.
*(doctrine-names-are-claims. → `[task:version-skew-watch]`.)*

## 2.20.2 — 2026-08-11 — the boot block states how many units it handed over

**Problem:** the cut manifest reported per-section *withholding* — which is
policy — and never the size of the payload, which is capacity. Opie's boot
under 2.20.0 truncated his lane because `opie.md` is 33,984 on disk against a
12,500 section cap; it would have truncated **identically** whether the
`BOOT_TARGET` change had landed or silently not. A report that reads the same
under both outcomes is not a check (#2355).

**Fix:** every boot block now ends with
`📏 BOOT PAYLOAD: N units delivered against BOOT_TARGET T`, counted by the
producer after assembly. Fixed-point, because the line reports a length it is
part of — converges in 3 passes, verified `reported == actual`, and prints
`>=` rather than asserting a figure it did not verify if it ever fails to.

⚠️ **Stated limit, written into the line itself:** it reports what the host was
**handed**, never what the host **accepted**. It closes "did the budget change
take effect." It cannot close "did the host survive it."

## 2.20.1 — 2026-08-11 — REVERT of 2.20.0: measured on one host, spent across five

**Problem:** 2.20.0 raised `BOOT_TARGET` to 49,000 on a limit read out of the
**Claude Code** binary. `boot-budget.js` is fleet-wide: Opie runs Claude
Desktop, Rocky Hermes, Dave OpenClaw, Cody Codex CLI. Four hosts with their own
inline-result limits, none measured. The measurement was sound and its **scope**
was not.

**Fix:** all values reverted (`BOOT_TARGET` 45,000; `lane` 11,000, `mnemo`
2,000, `doctrines.md` 5,500, `CLAUDE.md` 6,500, `people.md` 2,000).

**Two corrections on the record.** The revert was made on a *causal* claim — a
10-minute stall with two maxed tool uses, attributed to the raise — that the
evidence does not support: Opie's session **booted cleanly under 49,000**, and
"maxed tool use" is a tool-*call-count* ceiling while boot payload spends
*context*. No mechanism connects them. **The revert is right on precaution and
never needed the causation.** And the argument for raising rested on 45,000
being arbitrary, where the only evidence for "arbitrary" was that no
justification had been written down — **an undocumented constraint is not an
unjustified one**, and precaution independently arrives at the same number.

**Re-raising requires the two-ended test PER HOST, with the fleet value set to
the MINIMUM across them.**

## 2.20.0 — 2026-08-11 — the boot ceiling was a guess, and the guess cost 28,000 characters
*(⛔ superseded by 2.20.1 the same hour — kept for the measurement, which is still valid for Claude Code specifically.)*

**Problem:** `BOOT_TARGET` was 45,000 and had never been measured. It was
inferred from a single incident — a 73KB boot block diverting to a file on
2026-07-09 — and then set far enough below it to feel safe. Every agent has
been paying for that margin daily ever since: budgets fleet-wide sat within
~200 units of their caps, boot-file additions had to be traded against
removals, and CC's boot on 2026-08-11 withheld 1,946 characters — 49% of one
section — against a ceiling nobody had checked. The number that was throttling
five agents' continuity was a round number someone picked once.

**What the host actually does** (read out of the Claude Code 2.1.227 binary,
then confirmed empirically): the cap is `MAX_MCP_OUTPUT_TOKENS`, default
**25,000 tokens — not characters**, applied in two stages. Stage 1 is a cheap
estimate, `Math.round(text.length / 4)`, compared against `25,000 * 0.5`; pass
it and the result is returned inline **with no further check**. That makes
**50,000 UTF-16 units a hard guarantee regardless of token density**. Only
above 50,000 chars does it run a real token count and divert if that exceeds
25,000. Both units are `String.length`, so the bridge and the host measure the
same thing — the old UTF-8-vs-UTF-16 caveat in `boot-budget.js` does not apply.

**Confirmed at both ends:** 44,800 chars (that morning's boot) → inline.
73,103 chars (`active-archive-2026-Q2.md` via `read_brain_file`) → "exceeds
maximum allowed tokens", saved to a file — reproducing the 2026-07-09 incident
exactly and putting our content under ~2.9 chars/token. Markdown, emoji and
code fences are token-expensive, so the naive `length / 4` estimate flatters
us; that is the reason to stop at the stage-1 guarantee rather than chase the
true stage-2 ceiling near ~70,000. Stage 1 cannot be tipped by an emoji-heavy
session. Stage 2 can.

**Fix:** `BOOT_TARGET` 45,000 → **49,000** (1,000 units below the guarantee;
worst case computes to 48,450, so 1,550 of real margin). The +4,000 is
distributed where the cuts were landing, not evenly: `lane` 11,000 → 12,500
(all five agents sat within ~200 units of it), `mnemo` 2,000 → 3,300 (the
section CC's boot cut 49% off), `doctrines.md` 5,500 → 6,000, `CLAUDE.md`
6,500 → 7,000, `people.md` 2,000 → 2,200. **`active.md` deliberately gets
nothing** — `board-check.py` already forces it to shrink, and relieving that
pressure would remove a constraint that is doing useful work.

**Effect:** every lane green with margin. CC's lane went from 11 units of
headroom to 1,511 — it had been one edit from a silent cut all week.

⚠️ **Inert until each client restarts.** A bridge change does not reach a
running session.

## 2.19.1 — 2026-08-04 — share-mode search never leaves the bridge unscoped

**Problem:** The server answers agent-less `/context` requests with a silent
empty 200 (bus #1941). In share mode (`MNEMO_SHARE=always`, or any session
where `mnemo_share` was toggled on), `mnemo_search`
with no explicit `agent_id` sent no `agent_id` at all — so every unscoped
search returned zero results under green health. A share-mode Desktop
session lost a whole afternoon of search capability to this, and the zeros
read as "the store is empty," nearly deleting five specimens' last copy.

**Fix:** Every search now names a tenant (new `search-scope.js`, with
regression tests). Share mode + explicit `agent_id` → that agent, unchanged.
Share mode + no `agent_id` → self-scoped, and the result says so in a
prefix instead of silently narrowing. Separate mode unchanged. When the
server ships its explicit absent-agent_id contract (the server half of
#1941), re-enabling true unscoped search is a deliberate change, not a
cleanup. Bridge changes are inert until the host app restarts.

## 2.19.0 — 2026-07-30 — nothing gets cut silently: cut manifest at the top of every boot

*(Entry backfilled 2026-08-04 — `529417f` shipped with a root CHANGELOG
entry but none here.)*

**Problem:** `capSection` announced truncation at the END of the section it
truncated — inside the payload, where readers skim past. CC and Opie
received those notices for 20+ days and neither acted on one. Measured on
IGOR 2026-07-30: 87% of every boot (235,684 of 270,538 chars) was being
dropped invisibly. Guy's rule: "Nothing gets cut! If something is going to
be cut then I am notified before any more."

**Fix:** Cuts are collected per boot and reported at the TOP of the block,
where they cannot themselves be truncated, and appended to
`boot-cuts.jsonl` so growth is diffable. Emitted on clean boots too — a
report that only appears when something is wrong cannot be told apart from
a reporter that has stopped working.

## 2.18.2 — 2026-07-17 — boot no longer trips the Thesaurus Loop

**Problem:** The `agent_startup` boot block's "recent Mnemo context" section
timed out three sessions in a row. The boot's `/context` query ("recent
session summary, current projects, what happened last") is deliberately
broad, so as the agent's corpus grows its top hits score flat — exactly the
escalation condition for Thesaurus Loop query expansion (server v4.2).
Expansion turns one retrieval pass into an LLM call plus several extra
passes with L3 disk walks: 25s measured against the live Cortex, vs the
bridge's 10s `FETCH_TIMEOUT_MS`. Once the corpus crossed the flatness
threshold, every boot lost its memories section.

**Fix:** The boot call now passes `expand: false` — the server API's own
contract says expansion is live-path only and background loads should opt
out. Interactive recall (`mnemo_recall`, `mnemo_search`) keeps expansion.
Same query measured at 0.37s with expansion off.

## 2.18.1 — 2026-07-17 — an agent can write its own protected lane again

**Problem:** `write_brain_file` refused `cc-session.md` for *every* agent —
including the `cc` agent whose lane it is. The hardcoded blocklist
(`["cc-session.md", "CLAUDE.md"]`) dates from the bridge's OpenClaw-only era,
when protecting another agent's lane from its callers was the right shape.
Once the same bridge served all agents, the owner was locked out of its own
lane through the tool — surfaced by the first real post-2.18.0 lane write,
which auto-commit made the preferred path.

**Fix:** New `lane-guard.js`: lane-protected files are allowed when the
filename matches the calling agent's own lane candidates (`<agent>.md` /
`<agent>-session.md` — the same convention startup uses); every other agent
stays refused. `CLAUDE.md` is refused unconditionally (so a spoofy
`MNEMO_AGENT_ID=CLAUDE` can't unlock the operating doc — edit it deliberately
on disk instead). Five-test suite (`lane-guard.test.js`).

## 2.18.0 — 2026-07-17 — write_brain_file auto-commits and pushes

**Problem:** A lane file written via `write_brain_file` was invisible to the
rest of the fleet until `session_end` (or a human) committed it. Long sessions
that never reach `session_end` stranded the write on local disk — one agent's
lane rewrite sat uncommitted for two days while its own text noted the repo
was behind. The commit was a polite convention, not a hard check.

**Fix:** `write_brain_file` now stages, commits, and pushes the written file
immediately (new `brain-git.js`). Pathspec commit — only the named file, so
unrelated staged work is never swept up. Fail-soft: the write itself never
errors on git trouble; the tool response appends a status line
(`auto-committed + pushed` / `no changes` / `push FAILED (…)`) so the agent
knows whether follow-up is needed. The push carries a 15s timeout so a
network stall degrades to the fail-soft status instead of freezing the tool.
`session_end`'s commit is unchanged and now acts as the backstop. Six-test
suite (`brain-git.test.js`) against real throwaway repos: push round-trip,
no-empty-commit, pathspec isolation, push-failure reporting, the production
BRAIN_DIR-as-repo-subdir shape, non-repo skip.

## 2.17.0 — 2026-07-09 — Harness tool allow-list is enforced at registration

**Problem:** `HARNESS_ENABLED_TOOLS` was advisory: the bridge still registered
every tool it discovered, so a harness configured for a narrow capability set
received the full MCP surface.

**Fix:** The existing `server.registerTool` wrapper now blocks every tool absent
from the comma-separated allow-list before it reaches the MCP SDK. Unset or empty
configuration retains the existing register-everything behavior. With filtering
active, startup emits one stderr notice listing skipped tools and warns without
crashing when no known tool matches. Registration-gate unit tests cover unfiltered,
subset, and unknown-only configurations.

## 2.16.0 — 2026-07-09 — Per-section byte budgets: the boot block lands inline again

**Problem:** `agent_startup` capped each brain file at a flat 40KB but left the
dream brief and Mnemo context uncapped and the TOTAL unbounded. CC's boot hit
73KB on 2026-07-09 and diverted to a file instead of landing inline (the MCP
host caps inline tool results at roughly 45KB) — every session started with a
subagent digest instead of a readable boot block.

**Fix:** New `boot-budget.js` gives every boot section its own byte budget
(lane 11K, CLAUDE.md 6.5K, active.md 10K, people.md 2K, doctrines.md 5.5K,
Mnemo context 2K, dream brief 3.5K), sized so the worst-case total — all
sections maxed plus header/freshness/separator overhead — stays under the 45KB
target. Files are newest-first/priority-first so the kept top slice is the
right slice; every truncation notice names the tool that re-reads the full
content (`read_brain_file`, `mnemo_recall`, `/dream/latest`). Unit tests in
`boot-budget.test.js` include a budget-sum invariant so a future budget bump
can't silently push the boot back over the inline cap. Verified end-to-end
over real MCP stdio: CC's boot went 73,185 → 40,360 bytes, inline.

## 2.15.2 — 2026-07-08 — Wiki tool descriptions relabeled as legacy; the Librarian is the discovery system

**Problem:** The wiki tool descriptions still sold `wiki_search` as the primary way
to find documents — "indexed project docs, session transcripts, entities, and
concepts", implying a live, maintained knowledge base. The nightly wiki compile was
retired 2026-07-07 when the Librarian (an SQLite FTS5 index over the whole
workspace, queried via FrankenClaw's `file_find`) replaced it. Agents reading the
old descriptions would reach for the wrong tool and trust stale pages as current.

**Fix:** `wiki_search` / `wiki_read` / `wiki_index` descriptions now say what the
pages actually are — a legacy WikAI snapshot, no longer recompiled — and point live
document discovery at `file_find`. Tool behavior is unchanged; the static pages
remain fully searchable.

## 2.15.1 — 2026-07-05 — Lane freshness on EVERY boot, not just past a threshold

**Problem:** 2.15.0 only spoke up after 7 silent days. Guy, same evening: "Every agent
every time is more like what I want. I notice when the last session is missing." A
7-day gate means a lane can quietly drop 6 days of sessions before anyone is told.

**Fix:** The boot block now leads with lane freshness unconditionally: a one-line
`LANE FRESHNESS` note (last-commit date + keep-the-streak reminder) when the lane is
current, escalating to the `⚠️ YOUR LANE FILE IS BEHIND` banner once the last commit
is older than 1 day — i.e., the moment a session is missing. The session_end advisory
is unchanged (it already fired every time).

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
