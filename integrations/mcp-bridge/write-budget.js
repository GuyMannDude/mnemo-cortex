// Boot-budget check for brain writes.
//
// `agent_startup` caps every boot-loaded file at its STARTUP_BUDGETS slice
// and keeps the HEAD — the tail is dropped. Until now nothing measured a
// lane at the moment it was WRITTEN, so an agent learned it had overrun
// only at the next boot, from a manifest inside the payload, one session
// too late to remember what the dropped tail said. It failed on 3 of 9
// days (2026-08-03/04/11); the 08-11 specimen cut opie.md 976 units past
// the cap and nothing said so until the following morning.
//
// The gate belongs on the ACTION (doctrine-breathing-muscle-jazz): the
// write is where the overrun is created and where the author still knows
// which lines are worth keeping.
//
// ⚠️ THIS NEVER BLOCKS A WRITE. A refused lane write at session end loses
// the update outright, which is strictly worse than an oversized lane —
// the tail is dropped at BOOT, not on disk, and `read_brain_file` still
// returns it whole. Write, then scream (doctrine-degrade-to-raw).
//
// Semantics are lane-check.py's, deliberately: same budget source, same
// UTF-16 ruler, same BOOT BOUNDARY rule, same margin floor. Two tools that
// disagree about whether a lane is healthy are worse than one.

import { STARTUP_BUDGETS, MARGIN_FLOOR } from "./boot-budget.js";

// Which budget slice a written file will be capped against at boot, or
// null when the file is not boot-loaded (snags, doctrine bodies, archives
// — the overwhelming majority of brain writes, which must stay silent; a
// guard that fires on healthy writes teaches its reader to skip it).
// `ownedLanes` is the agent's lane-candidate list (lane-candidates.js) —
// tenant-named, or the MNEMO_LANE override — so a lane that is not named
// after the tenant is still budget-checked.
export function budgetKeyFor(filename, ownedLanes) {
  if (ownedLanes && ownedLanes.includes(filename)) {
    return "lane";
  }
  // Shared boot-loaded docs are keyed by their own filename. Restricted to
  // *.md so the non-file keys ("lane", "mnemo", "dream") can never match.
  if (filename.endsWith(".md") && filename in STARTUP_BUDGETS) return filename;
  return null;
}

// Offset of the BOOT BOUNDARY heading line, in UTF-16 units, or null.
//
// Must match the heading LINE, not the first mention of the phrase: a lane
// that states its own ordering rule ("...goes above the BOOT BOUNDARY
// marker") in prose would otherwise report that position and look like it
// had thousands of units to spare. Same failure lane-check.py documents.
export function findBoundary(text) {
  let offset = 0;
  for (const line of text.split("\n")) {
    if (line.trimStart().startsWith("#") && line.includes("BOOT BOUNDARY")) {
      return offset;
    }
    offset += line.length + 1;
  }
  return null;
}

// Assess written content against the slice it will be capped to.
// Returns { status, ... } — status is one of:
//   ok     — fits, with margin
//   tight  — a LANE that fits, but under MARGIN_FLOOR units of headroom
//   bound  — a LANE over budget ON PURPOSE (BOOT BOUNDARY above the cap)
//   lies   — a LANE whose BOOT BOUNDARY sits PAST the real cut
//   cut    — over budget, and the overrun is NOT declared
//
// ⚠️ TIGHT, BOUND and LIES are LANE-ONLY, deliberately. The shared
// boot-loaded docs belong to boot-budget-check.py, which applies a
// PROPORTIONAL floor (`max(100, budget // 20)`, Opie #2011) — a flat 500
// against people.md's 2,000 budget is 25% of the file and would scream
// TIGHT at a doc that gate calls healthy and that boots whole. Rather than
// re-home that rule here and create the second copy this module exists to
// avoid, shared files report only the one verdict both tools agree on
// without arithmetic: over budget, or not.
export function assess({ filename, content, ownedLanes }) {
  const key = budgetKeyFor(filename, ownedLanes);
  if (!key) return null;
  const isLane = key === "lane";
  const budget = STARTUP_BUDGETS[key];
  const total = content.length;
  const boundary = isLane ? findBoundary(content) : null;

  if (total <= budget) {
    const headroom = budget - total;
    return {
      status: isLane && headroom < MARGIN_FLOOR ? "tight" : "ok",
      key, filename, budget, total, headroom, dropped: 0,
    };
  }

  const dropped = total - budget;
  // A boundary at or above the cap means everything below it is reference
  // the owner chose not to ship. That is a correctly-structured lane, not
  // a fault — but the SPARE between boundary and cap still has a floor,
  // because growth above the line eats it silently.
  if (boundary !== null && boundary <= budget) {
    const spare = budget - boundary;
    return {
      status: "bound",
      key, filename, budget, total, dropped, boundary, spare,
      tight: spare < MARGIN_FLOOR,
    };
  }

  // The LIES class, named separately from CUT for the reason lane-check.py
  // gives: the owner of a CUT lane knows they have no boundary; the owner
  // of a LYING one believes theirs. The marker asserts "everything above me
  // boots" while sitting past the real cut, so content the owner ranked
  // operational is dropped and the file claims it shipped.
  if (boundary !== null && boundary > budget) {
    return {
      status: "lies",
      key, filename, budget, total, dropped, boundary,
      past: boundary - budget,
    };
  }

  return {
    status: "cut",
    key, filename, budget, total, dropped,
    pct: Math.round((100 * dropped) / total),
    firstLineCut: content.slice(budget).split("\n").find((l) => l.trim()) || "",
  };
}

function clip(s, n = 90) {
  const flat = s.trim();
  return flat.length <= n ? flat : flat.slice(0, n - 3) + "...";
}

// Human-facing line(s) for a write result, or null when there is nothing
// worth saying. Silence is the common case and is the point.
export function budgetWarning({ filename, content, ownedLanes }) {
  const a = assess({ filename, content, ownedLanes });
  if (!a) return null;

  if (a.status === "cut") {
    return (
      `🚨 OVER BOOT BUDGET — ${a.filename} is ${a.total.toLocaleString()} UTF-16 units ` +
      `against a ${a.budget.toLocaleString()} cap.\n` +
      `${a.dropped.toLocaleString()} units (${a.pct}%) will be SILENTLY DROPPED from every ` +
      `boot that loads it, starting at:\n` +
      `    ${clip(a.firstLineCut)}\n` +
      `The write SUCCEEDED and the disk file is whole — the loss happens at boot, and ` +
      `nothing will announce it again until then. Trim now, this session, while you still ` +
      `know which lines matter. Gate: brain/tools/lane-check.py --check`
    );
  }

  if (a.status === "lies") {
    return (
      `🚨 BOOT BOUNDARY IS PAST THE REAL CUT — ${a.filename}'s marker sits ` +
      `${a.past.toLocaleString()} units beyond the ${a.budget.toLocaleString()} cap.\n` +
      `The marker asserts everything above it boots. It does not: the band between the ` +
      `cap and the marker is content you ranked operational, dropped every boot, in a ` +
      `file that says it shipped. Move the marker back under the cap or trim above it.`
    );
  }

  if (a.status === "tight") {
    return (
      `⚠️ ${a.filename}: ${a.headroom.toLocaleString()} units of headroom against the ` +
      `${a.budget.toLocaleString()} boot cap — under the ${MARGIN_FLOOR}-unit floor, ` +
      `one edit from a silent cut. Trim while it is cheap.`
    );
  }

  if (a.status === "bound" && a.tight) {
    return (
      `⚠️ ${a.filename}: BOOT BOUNDARY is only ${a.spare.toLocaleString()} units under the ` +
      `${a.budget.toLocaleString()} cap (floor ${MARGIN_FLOOR}) — growth above the line will ` +
      `start cutting content you meant to ship.`
    );
  }

  return null;
}
