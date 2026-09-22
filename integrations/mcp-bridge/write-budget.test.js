// Tests for write-budget.js — the boot-budget check on brain writes.
// Run: node write-budget.test.js
//
// Style matches lane-guard.test.js / boot-budget.test.js: homemade runner,
// plain console output, exit 1 on any failure.

import { STARTUP_BUDGETS, MARGIN_FLOOR } from "./boot-budget.js";
import { budgetKeyFor, findBoundary, assess, budgetWarning } from "./write-budget.js";
import { laneCandidates } from "./lane-candidates.js";

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  PASS  ${name}`);
    passed++;
  } catch (err) {
    console.log(`  FAIL  ${name}: ${err.message}`);
    failed++;
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}

const LANE = STARTUP_BUDGETS.lane;
const filler = (n) => "x".repeat(n);

console.log("\n── budgetKeyFor: which writes are even measured ──\n");

test("an agent's own lane resolves to the lane budget", () => {
  assert(budgetKeyFor("cc-session.md", laneCandidates("cc")) === "lane");
  assert(budgetKeyFor("opie.md", laneCandidates("opie")) === "lane");
  // MNEMO_LANE override: a lane not named after the tenant is still a lane.
  assert(budgetKeyFor("cc2-igor2.md", laneCandidates("cc", "cc2-igor2.md")) === "lane");
  assert(budgetKeyFor("cc-session.md", laneCandidates("cc", "cc2-igor2.md")) === null);
});

test("shared boot-loaded docs resolve to their own key", () => {
  assert(budgetKeyFor("active.md", laneCandidates("cc")) === "active.md");
  assert(budgetKeyFor("doctrines.md", laneCandidates("opie")) === "doctrines.md");
});

test("files that are NOT boot-loaded are silent — the common case", () => {
  assert(budgetKeyFor("snag-whatever.md", laneCandidates("cc")) === null);
  assert(budgetKeyFor("cc-s220.md", laneCandidates("cc")) === null);
  assert(budgetKeyFor("incidents.md", laneCandidates("cc")) === null);
  assert(budgetKeyFor("stack.md", laneCandidates("cc")) === null);
});

test("the non-file budget keys can never be matched by a filename", () => {
  // STARTUP_BUDGETS has "lane", "mnemo", "dream" — none are files on disk.
  assert(budgetKeyFor("lane", laneCandidates("cc")) === null);
  assert(budgetKeyFor("mnemo", laneCandidates("cc")) === null);
  assert(budgetKeyFor("dream", laneCandidates("cc")) === null);
});

test("another agent's lane is not measured against MY lane budget", () => {
  // CC may correct structure in another lane (the Lane Protocol carve-out),
  // but the ruler for that is lane-check.py <owner>, not this guard.
  assert(budgetKeyFor("opie.md", laneCandidates("cc")) === null);
});

console.log("\n── findBoundary: the marker locator ──\n");

test("finds the BOOT BOUNDARY heading line", () => {
  const text = "# Lane\n\nstuff\n\n## BOOT BOUNDARY\n\nreference\n";
  assert(findBoundary(text) === text.indexOf("## BOOT BOUNDARY"));
});

test("ignores a PROSE mention and finds the real heading", () => {
  // The specimen that made a 19K lane look like it had 10,741 units spare.
  const prose = "# Lane\n\nOrdering rule: anything below goes above the BOOT BOUNDARY marker.\n";
  const text = prose + "\nbody\n\n## BOOT BOUNDARY\n\ntail\n";
  assert(findBoundary(text) === text.indexOf("## BOOT BOUNDARY"),
    "matched the prose mention instead of the heading");
});

test("returns null when there is no marker", () => {
  assert(findBoundary("# Lane\n\njust content\n") === null);
});

console.log("\n── assess: the four states ──\n");

test("a lane with margin is ok and warns nothing", () => {
  const a = assess({ filename: "cc-session.md", content: filler(LANE - 5000), ownedLanes: laneCandidates("cc") });
  assert(a.status === "ok", `expected ok, got ${a.status}`);
  assert(a.dropped === 0);
  assert(budgetWarning({ filename: "cc-session.md", content: filler(LANE - 5000), ownedLanes: laneCandidates("cc") }) === null);
});

test("a lane inside the floor is TIGHT and says so before any damage", () => {
  const content = filler(LANE - (MARGIN_FLOOR - 1));
  const a = assess({ filename: "cc-session.md", content, ownedLanes: laneCandidates("cc") });
  assert(a.status === "tight", `expected tight, got ${a.status}`);
  assert(a.dropped === 0, "a tight file is not yet cut");
  const w = budgetWarning({ filename: "cc-session.md", content, ownedLanes: laneCandidates("cc") });
  assert(w && w.includes("floor"), "tight warning must name the floor");
});

test("exactly at the budget fits — capSection cuts only when OVER", () => {
  // capSection: `if (text.length <= budget) return text`. Off-by-one here
  // would report a cut that never happens.
  const a = assess({ filename: "cc-session.md", content: filler(LANE), ownedLanes: laneCandidates("cc") });
  assert(a.dropped === 0, "a file exactly at budget is delivered whole");
  assert(a.status === "tight", "…but it has zero headroom, so: tight");
});

test("one unit over is CUT, and the count matches capSection exactly", () => {
  const a = assess({ filename: "cc-session.md", content: filler(LANE + 1), ownedLanes: laneCandidates("cc") });
  assert(a.status === "cut", `expected cut, got ${a.status}`);
  assert(a.dropped === 1, `expected 1 dropped, got ${a.dropped}`);
});

test("the 08-11 specimen: 976 units past the cap reports 976", () => {
  const a = assess({ filename: "opie.md", content: filler(LANE + 976), ownedLanes: laneCandidates("opie") });
  assert(a.status === "cut");
  assert(a.dropped === 976, `expected 976, got ${a.dropped}`);
  const w = budgetWarning({ filename: "opie.md", content: filler(LANE + 976), ownedLanes: laneCandidates("opie") });
  assert(w.includes("976"), "the warning must carry the real number");
  assert(w.includes("SILENTLY DROPPED"), "the warning must be unmissable");
});

test("a declared BOOT BOUNDARY is deliberate, not a fault", () => {
  // Opie's live shape: 32K on disk, marker well under the cap. Screaming at
  // this every write is the guard-that-is-wrong-every-day failure.
  const content = filler(LANE - 5000) + "\n## BOOT BOUNDARY\n" + filler(20_000);
  const a = assess({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") });
  assert(a.status === "bound", `expected bound, got ${a.status}`);
  assert(budgetWarning({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") }) === null,
    "a correctly-structured lane must warn nothing");
});

test("a boundary crowding the cap still warns — spare has a floor too", () => {
  const content = filler(LANE - (MARGIN_FLOOR - 1)) + "\n## BOOT BOUNDARY\n" + filler(20_000);
  const a = assess({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") });
  assert(a.status === "bound");
  assert(a.tight === true, "spare under the floor must be flagged");
  const w = budgetWarning({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") });
  assert(w && w.includes("BOOT BOUNDARY"), "warning must name the boundary");
});

test("a marker BEYOND the cap does not excuse the cut", () => {
  // Marker at 20K with an 11K cap: content between cap and marker is claimed
  // as shipped but never boots. Not a declared cut — it is the LIES class,
  // and the one thing that must never happen is it being silenced as BOUND.
  const content = filler(20_000) + "\n## BOOT BOUNDARY\n" + filler(500);
  const a = assess({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") });
  assert(a.status !== "bound", "a marker past the cap must never read as deliberate");
  assert(a.status === "lies", `expected lies, got ${a.status}`);
  assert(budgetWarning({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") }) !== null);
});

console.log("\n── lane-only semantics: the shared docs are not ours to grade ──\n");

test("a shared doc inside the flat floor is OK, not TIGHT — the live people.md case", () => {
  // people.md: 1,722 of a 2,000 budget = 278 spare. boot-budget-check.py owns
  // it and uses a PROPORTIONAL floor (max(100, budget//20) = 100) -> healthy.
  // A flat 500 here would scream TIGHT at 25% of the whole budget, every write,
  // on a file that boots whole and that the authoritative gate calls ok.
  const a = assess({ filename: "people.md", content: filler(1722), ownedLanes: laneCandidates("cc") });
  assert(a.status === "ok", `expected ok, got ${a.status}`);
  assert(budgetWarning({ filename: "people.md", content: filler(1722), ownedLanes: laneCandidates("cc") }) === null,
    "a healthy shared doc must warn nothing");
});

test("a shared doc with thin margin is still OK — CLAUDE.md's live 360 spare", () => {
  const budget = STARTUP_BUDGETS["CLAUDE.md"];
  const a = assess({ filename: "CLAUDE.md", content: filler(budget - 360), ownedLanes: laneCandidates("cc") });
  assert(a.status === "ok", `expected ok, got ${a.status}`);
});

test("a shared doc genuinely OVER budget still reports — both gates agree there", () => {
  const budget = STARTUP_BUDGETS["active.md"];
  const a = assess({ filename: "active.md", content: filler(budget + 40), ownedLanes: laneCandidates("cc") });
  assert(a.status === "cut", `expected cut, got ${a.status}`);
  assert(a.dropped === 40);
});

test("a BOOT BOUNDARY in a shared doc does NOT excuse an overrun", () => {
  // BOUND is lane semantics. A marker in active.md must not silently bless a
  // 20K board that boot-budget-check.py reports as [FAIL] over budget.
  const budget = STARTUP_BUDGETS["active.md"];
  const content = filler(budget - 500) + "\n## BOOT BOUNDARY\n" + filler(20_000);
  const a = assess({ filename: "active.md", content, ownedLanes: laneCandidates("cc") });
  assert(a.status === "cut", `expected cut, got ${a.status}`);
  assert(budgetWarning({ filename: "active.md", content, ownedLanes: laneCandidates("cc") }) !== null,
    "an over-budget shared doc must not be silenced by a marker");
});

console.log("\n── the LIES class: a marker that sits past the real cut ──\n");

test("a marker PAST the cap is LIES, not CUT — the owner believes their boundary", () => {
  // Three live specimens: opie.md 08-01 (976 past), 08-06 (1,905), 08-11 (983).
  const content = filler(LANE + 976) + "\n## BOOT BOUNDARY\n" + filler(500);
  const a = assess({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") });
  assert(a.status === "lies", `expected lies, got ${a.status}`);
  assert(a.past === 976 + 1, `expected the marker 977 past the cap, got ${a.past}`);
  const w = budgetWarning({ filename: "opie.md", content, ownedLanes: laneCandidates("opie") });
  assert(w.includes("PAST THE REAL CUT"), "the LIES warning must name its own class");
});

test("LIES is distinguishable from CUT — same failure, different diagnosis", () => {
  const noMarker = assess({ filename: "opie.md", content: filler(LANE + 976), ownedLanes: laneCandidates("opie") });
  assert(noMarker.status === "cut", "no marker at all is CUT");
  // Both fail; the owner needs to know WHICH, because the remedies differ.
  assert(noMarker.status !== "lies");
});

console.log("\n── the ruler: UTF-16 units, not bytes or code points ──\n");

test("counts UTF-16 units, matching what capSection slices on", () => {
  // An emoji is 4 bytes, 1 code point, but 2 UTF-16 units. Byte-counting
  // over-estimates and code-point-counting under-estimates; only .length
  // matches the cut. These lanes are emoji-dense, so this is not academic.
  const emoji = "🚨";
  assert(emoji.length === 2, "sanity: astral char is 2 UTF-16 units");
  const content = emoji.repeat(LANE / 2 + 1); // 2 units over
  const a = assess({ filename: "cc-session.md", content, ownedLanes: laneCandidates("cc") });
  assert(a.status === "cut");
  assert(a.dropped === 2, `expected 2 units dropped, got ${a.dropped}`);
});

test("shared docs are measured against their OWN budget, not the lane's", () => {
  const board = STARTUP_BUDGETS["active.md"];
  assert(board !== LANE, "sanity: the board budget differs from the lane budget");
  const a = assess({ filename: "active.md", content: filler(board + 10), ownedLanes: laneCandidates("cc") });
  assert(a.status === "cut");
  assert(a.budget === board, `measured against ${a.budget}, expected ${board}`);
  assert(a.dropped === 10);
});

console.log("\n── the standing rule: never block the write ──\n");

test("budgetWarning only ever returns text — it has no refusal path", () => {
  // A refused lane write at session end loses the update outright. The
  // contract is write-then-scream; if this ever gains a boolean, the
  // caller in server.js must be re-read.
  const over = budgetWarning({ filename: "cc-session.md", content: filler(LANE * 3), ownedLanes: laneCandidates("cc") });
  assert(typeof over === "string", "an overrun yields a message, not a refusal");
  assert(over.includes("SUCCEEDED"), "the message must say the write landed");
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
