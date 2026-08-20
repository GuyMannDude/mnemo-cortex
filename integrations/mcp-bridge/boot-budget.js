// Per-section byte budgets for the agent_startup boot block.
//
// The old scheme capped each brain file at a flat 40KB, left the dream
// brief and Mnemo context uncapped, and let the total float — CC's boot
// hit 73KB on 2026-07-09 and diverted to a file instead of landing
// inline (the MCP host caps inline tool results; ~45KB total is safely
// under it). Every section now has its own byte budget, sized so the
// WORST-CASE total (all sections maxed + header/freshness/separator
// overhead) stays below BOOT_TARGET. Anything cut is one tool call away
// — the truncation notice says exactly which tool re-reads it in full.

// Budgets count UTF-16 code units (.length), not UTF-8 bytes. This turns out to
// be exactly right rather than approximately right: the host measures the same
// way (see below), so the two sides agree unit-for-unit and the old worry about
// UTF-8 divergence does not apply.
//
// ── MEASURED 2026-08-11 (S218), replacing a guess ──────────────────────────
// 45,000 was never measured. It was inferred from one incident (a 73KB boot
// diverting to a file on 2026-07-09) and set low enough to feel safe, which
// cost every agent real continuity every morning — CC's boot on 2026-08-11
// withheld 1,946 chars, 49% of one section, against a ceiling nobody had
// checked.
//
// The host's actual rule, read out of the Claude Code binary (2.1.227) and
// confirmed empirically:
//
//   MAX_MCP_OUTPUT_TOKENS defaults to 25,000 TOKENS — not characters.
//   The check runs in two stages:
//     1. A cheap estimate, Math.round(text.length / 4). If that is <= 25,000
//        * 0.5 = 12,500, the result is returned inline WITHOUT ANY FURTHER
//        CHECK. That makes 50,000 UTF-16 units a hard guarantee, independent
//        of how token-dense the content is.
//     2. Above 50,000 chars it performs a REAL token count and diverts to a
//        file only if that exceeds 25,000 tokens.
//
// Empirical confirmation, both ends:
//   44,800 chars (that morning's boot)        -> inline
//   73,103 chars (active-archive-2026-Q2.md)  -> "exceeds maximum allowed
//                                                tokens", saved to a file
// The 73,103 result reproduces the 2026-07-09 incident exactly, and puts our
// content's real density under ~2.9 chars/token — markdown, emoji and code
// fences are expensive, so the naive length/4 estimate FLATTERS us. That is
// precisely why we stop at the stage-1 guarantee instead of chasing the true
// stage-2 ceiling near ~70,000: stage 1 cannot be tipped by a session that
// happens to be emoji-heavy, and stage 2 can.
//
// ⛔ REVERTED TO 45,000 SAME DAY — THE MEASUREMENT WAS SOUND AND ITS SCOPE WAS NOT.
// Everything above was measured against the CLAUDE CODE host (2.1.227), which is
// CC's client. This file is FLEET-WIDE: Opie runs on Claude Desktop, Rocky on
// Hermes, Dave on OpenClaw, Cody on Codex CLI. Those are different hosts with
// their own inline-result limits, and none of them were measured. Raised to
// 49,000 at ~19:40; Opie's first session on it took ~10 minutes and maxed out
// tool use twice. Reverted immediately.
//
// The 45,000 that was called "a guess" may well have been calibrated to the
// TIGHTEST host rather than to CC's — in which case it was not a guess at all,
// it was a fleet minimum whose reasoning had been lost, and CC read the absence
// of a recorded justification as the absence of one.
//
// Re-raising requires the same two-ended measurement PER HOST, and the fleet
// value is then the MINIMUM across them, not CC's. → snag-boot-ceiling-was-a-guess.md
export const BOOT_TARGET = 45_000;

// Overhead outside the budgeted sections: identity header (~1.1KB),
// lane-freshness banner (~0.4KB), `\n\n---\n\n` separators, and the cut
// manifest (~0.9KB worst case — every section cut, measured not guessed).
// The manifest is counted here on purpose: it is emitted on every boot, so
// leaving it out of the invariant would let the block exceed BOOT_TARGET
// while the test that exists to prevent exactly that kept passing.
export const BOOT_OVERHEAD = 2_900;

// ⛔ The 2026-08-11 redistribution (lane 12,500 / mnemo 3,300 / doctrines 6,000
// / CLAUDE.md 7,000 / people.md 2,200) was REVERTED with BOOT_TARGET above.
// The allocation reasoning was fine; it was spending headroom measured on one
// host against a budget that five hosts share.
// ── 2026-08-12 (S220): 1,000 moved active.md -> doctrines.md. NET ZERO. ──
// This is NOT the 08-11 move repeated. That one raised BOOT_TARGET and spent
// headroom measured on one host against a budget five hosts share; Opie's next
// session maxed out tool use twice. This changes the SPLIT, not the SIZE: the
// budgets still sum to 40,500 and the test's worst-case boot is unchanged at
// 44,800, so no host — measured or unmeasured — sees a larger block than the
// one it has been booting on all along. A reallocation cannot regress a ceiling
// it does not move.
//
// Why: doctrines.md sat at 5,191/5,500 — 309 spare — and Opie STOPPED AUTHORING
// rather than write into a full store, with four doctrines queued behind it
// (incl. agreement-is-not-corroboration, #2382). Meanwhile active.md sat at
// 5,993/10,000 with 4,007 spare it has no route to using: the board is
// NO-GROWTH gated by board-check.py, so that headroom is reserved for growth
// that is deliberately forbidden. Idle budget behind a gate is not prudence,
// it is a stalled colleague.
//
// active.md keeps 3,007 of margin over its live size, and board-check.py still
// fails on any growth, so the 9,000 is not a ceiling the board can walk into.
// Reverting = swap the two numbers back; nothing else depends on them.
//
// ⚠️ THE 1,000 WAS BORROWED AGAINST A GATE, NOT AGAINST SLACK (Opie, #2391).
// Those 4,007 units were unspendable BECAUSE `board-check.py` NO-GROWTH gates
// active.md — not because the board is inherently small. **If that gate is ever
// relaxed, 9,000 binds where 10,000 would not have.** Whoever relaxes it is also
// spending doctrine headroom, and will not be told so by anything but this
// comment. Relax the gate -> restore 10,000/5,500 or re-measure both.
// ── 2026-08-20: 500 more moved active.md -> doctrines.md. NET ZERO, Guy-approved. ──
// Same shape as S220, same direction, same reason one wall later: the index hit
// FULL at 35 doctrines (321 spare vs the 325 MARGIN_FLOOR) and Opie's
// security-spending doctrine could not land without degrading eight other
// claim lines to buy 44 chars — clarity is the product of an index line, so
// trimming claims to fit new rules is a tax on the wrong asset (Opie #2781).
// Budgets still sum to 38,500; no host sees a larger block. ⚠️ #2391's caveat
// now covers 1,500 total: ALL of it is borrowed against board-check.py's
// NO-GROWTH gate. Relax that gate -> restore 10,000/5,500 or re-measure.
export const STARTUP_BUDGETS = {
  lane: 11_000,        // the agent's own continuity — biggest slice
  "CLAUDE.md": 6_500,  // cross-agent operating doc / session ritual
  "active.md": 8_500,  // the board; NO-GROWTH gated — lent 1,000 (S220) + 500 (08-20) to doctrines
  "people.md": 2_000,
  "doctrines.md": 7_000, // was 5,500; +1,000 (S220) +500 (08-20, Guy) so doctrine authoring never stalls
  // The "mnemo" key (worth 2,000 units) was RETIRED 2026-08-15 — deliberately
  // written WITHOUT the key:value shape, because boot-budget-check.py parses
  // this block by regex and a commented-out entry still counted.
  // The boot similarity section was CUT on
  // Guy's ruling and replaced with NOTHING; its 2,000 units are RECLAIMED, not
  // reassigned, so BOOT_TARGET is now that much less subscribed. Do not revive
  // this key to fund a "smaller" version — the ruling is removal.
  dream: 3_500,        // overnight dream brief
};

// Margin floor (Opie #1986, 2026-08-04): a file that fits with almost no
// headroom is one edit from a silent cut, and a gate that only reports the
// overrun afterwards reports it too late. Under this many units of spare,
// "fits" is downgraded to TIGHT.
//
// This lives HERE, beside the budgets, because two consumers need it and a
// constant with two homes drifts the moment one moves — exactly how
// lane-check.py came to hold a stale `BUDGET = 11_000` beside a comment
// correctly naming this file as its source (snag-lane-check-hardcoded-budget).
// Read by write-budget.js (import) and lane-check.py (parsed, via
// boot-budget-check.py). A comment naming a source is not a pointer.
export const MARGIN_FLOOR = 500;

// ── Cut audit (Guy's rule, 2026-07-30: "Nothing gets cut! New rule. If
// something is going to be cut then I am notified before any more.")
//
// Until now a cut announced itself only at the END of the section it
// truncated — i.e. inside the payload, ~11,000 chars into a lane, in the
// one place a reader skims past. Both CC and Opie received those notices
// for 20+ days and neither ever acted on one. An announcement buried in a
// payload nobody diffs is furniture, not an alarm.
//
// Cuts are now collected per boot and reported at the TOP of the block,
// where they cannot themselves be truncated, with an explicit instruction
// to tell Guy. The record is emitted whether or not anything was cut —
// "nothing was withheld" is a real result and is the only way to tell a
// healthy boot from a dead reporter.
let bootCuts = [];

export function beginBootAudit() {
  bootCuts = [];
}

export function getBootCuts() {
  return bootCuts.slice();
}

// Cap a boot-block section to its budget. Sections are ordered
// most-important-first (newest-first lanes, priority-first board), so
// keeping the top and cutting the tail loses the least. `hint` names
// the tool that fetches the full content. `label` attributes the cut in
// the manifest — without it a cut is recorded as "unnamed section",
// which is a bug worth seeing rather than hiding.
export function capSection(text, budget, hint, label) {
  if (text.length <= budget) return text;
  const withheld = text.slice(budget);
  const headings = [...text.matchAll(/^#{1,6}\s+(.+)$/gm)];
  const identifiers = headings
    .filter((m, index) => {
      const sectionEnd = index + 1 < headings.length ? headings[index + 1].index : text.length;
      return sectionEnd > budget;
    })
    .map((m) => m[1].trim())
    .concat(
      [...withheld.matchAll(/\b(?:memory_id|id)[:=]\s*([a-f0-9]{8,64})\b/gi)]
        .map((m) => m[1])
    );
  bootCuts.push({
    section: label || "unnamed section",
    actual: text.length,
    delivered: budget,
    dropped: text.length - budget,
    hint,
    dropped_identifiers: [...new Set(identifiers)],
  });
  return (
    text.slice(0, budget) +
    `\n\n…[truncated ${text.length - budget} of ${text.length} chars — ` +
    `top kept; ${hint}]…\n`
  );
}

// Render the manifest that leads the boot block. Kept deliberately small
// (~40 chars/section) so it never competes with the content it describes.
export function formatCutManifest() {
  if (bootCuts.length === 0) {
    return "✅ **BOOT COMPLETE — nothing was withheld from this boot.**";
  }
  const dropped = bootCuts.reduce((n, c) => n + c.dropped, 0);
  const actual = bootCuts.reduce((n, c) => n + c.actual, 0);
  const rows = bootCuts
    .map(
      (c) =>
        `| ${c.section} | ${c.actual.toLocaleString()} | ${c.delivered.toLocaleString()} | ` +
        `**${c.dropped.toLocaleString()}** | ${Math.round((100 * c.delivered) / c.actual)}% |`
    )
    .join("\n");
  return (
    `🚨 **${dropped.toLocaleString()} CHARACTERS WERE WITHHELD FROM THIS BOOT ` +
    `(${Math.round((100 * dropped) / actual)}% of ${bootCuts.length} file(s)).**\n\n` +
    `| section | actual | delivered | **withheld** | kept |\n` +
    `|---|---:|---:|---:|---:|\n${rows}\n\n` +
    `**Guy's standing rule (2026-07-30): he is to be NOTIFIED BEFORE ANY MORE IS CUT.** ` +
    `If this boot is the first you have seen these numbers, tell him — do not treat this ` +
    `table as boot furniture. You are reading a partial brain: anything you conclude from ` +
    `a file above may be contradicted by the part you were not given. ` +
    `\`read_brain_file\` fetches any of them in full.`
  );
}
