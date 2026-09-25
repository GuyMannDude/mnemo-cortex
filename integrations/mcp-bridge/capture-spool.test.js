// Tests for capture-spool.js — the on-disk mirror of the auto-capture buffer.
// No Mnemo server needed. Run: node capture-spool.test.js
//
// Style matches checkpoint.test.js: homemade runner, plain console output.

import { chmodSync, existsSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { spoolFile, persistSpool, parseSpool, orphanSpools, defaultPidAlive, SPOOL_PREFIX } from "./capture-spool.js";

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

function assertEq(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg || "mismatch"}: got ${a}, want ${e}`);
}

const STALE_MS = 240_000;
const DEAD = () => false;
const ALIVE = () => true;
const entry = (tool, summary) => ({ tool, summary, ts: "2026-09-21T17:40:00.000Z" });

// Age a file so orphanSpools sees it as untouched for `ageMs`.
function ageFile(file, ageMs) {
  const t = (Date.now() - ageMs) / 1000;
  utimesSync(file, t, t);
}

const dir = mkdtempSync(join(tmpdir(), "capture-spool-"));

console.log("\n── spoolFile ──\n");

test("name carries agent, pid and start time under the spool prefix", () => {
  const f = spoolFile("/state", "cc", 4242, 1700000000000);
  assertEq(f, "/state/capture-spool-cc-4242-1700000000000.jsonl");
});

test("agent id is made filename-safe", () => {
  const f = spoolFile("/state", "opie/cowork:2", 1, 5);
  assertEq(basename(f), "capture-spool-opie_cowork_2-1-5.jsonl");
});

test("two starts of the same pid get different files", () => {
  assert(spoolFile(dir, "cc", 7, 100) !== spoolFile(dir, "cc", 7, 101));
});

console.log("\n── persistSpool ──\n");

test("writes one JSON line per entry and creates the directory", () => {
  const f = spoolFile(join(dir, "fresh", "nested"), "cc", 1, 1);
  persistSpool(f, [entry("mnemo_recall", "a"), entry("write_brain_file", "b")]);
  const lines = readFileSync(f, "utf8").trimEnd().split("\n");
  assertEq(lines.length, 2);
  assertEq(JSON.parse(lines[1]).tool, "write_brain_file");
  assertEq(readdirSync(join(dir, "fresh", "nested")), [basename(f)], "no temp file left behind");
});

test("rewrite replaces, never appends", () => {
  const f = spoolFile(dir, "cc", 2, 1);
  persistSpool(f, [entry("a", "1"), entry("b", "2"), entry("c", "3")]);
  persistSpool(f, [entry("c", "3")]);
  assertEq(parseSpool(readFileSync(f, "utf8")).entries.map((e) => e.tool), ["c"]);
});

test("empty buffer removes the file", () => {
  const f = spoolFile(dir, "cc", 3, 1);
  persistSpool(f, [entry("a", "1")]);
  assert(existsSync(f), "precondition: file written");
  persistSpool(f, []);
  assert(!existsSync(f), "file should be gone");
});

test("empty buffer with no file is a no-op", () => {
  persistSpool(spoolFile(dir, "cc", 4, 1), []);
});

console.log("\n── parseSpool ──\n");

test("round-trips what persistSpool wrote", () => {
  const f = spoolFile(dir, "cc", 5, 1);
  const wrote = [entry("mnemo_recall", "x"), entry("wiki_read", "y")];
  persistSpool(f, wrote);
  assertEq(parseSpool(readFileSync(f, "utf8")), { entries: wrote, bad: 0 });
});

test("a torn or foreign line is counted, the rest survive", () => {
  const text =
    JSON.stringify(entry("a", "1")) + "\n" +
    '{"tool":"b","summ' + "\n" + // torn mid-write
    '{"nope":true}' + "\n" + // JSON, not an entry
    "\n" +
    JSON.stringify(entry("c", "3")) + "\n";
  const r = parseSpool(text);
  assertEq(r.entries.map((e) => e.tool), ["a", "c"]);
  assertEq(r.bad, 2);
});

test("empty text is empty, not an error", () => {
  assertEq(parseSpool(""), { entries: [], bad: 0 });
});

console.log("\n── orphanSpools ──\n");

test("missing state dir yields no orphans", () => {
  assertEq(orphanSpools(join(dir, "does-not-exist"), { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD }), []);
});

test("a fresh spool (live bridge) is not an orphan", () => {
  const d = mkdtempSync(join(dir, "o1-"));
  persistSpool(spoolFile(d, "cc", 1, 1), [entry("a", "1")]);
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD }).length, 0);
});

test("a stale spool is an orphan and carries its entries", () => {
  const d = mkdtempSync(join(dir, "o2-"));
  const f = spoolFile(d, "cc", 99, 1);
  persistSpool(f, [entry("read_brain_file", "machines.md"), entry("mnemo_recall", "q")]);
  ageFile(f, STALE_MS + 1000);
  const o = orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD });
  assertEq(o.length, 1);
  assert(o[0].file.startsWith(`${f}.claimed-${process.pid}-`), `returned under its claimed name: ${o[0].file}`);
  assert(!existsSync(f), "original name is gone");
  assert(existsSync(o[0].file), "claimed file exists for the caller to remove");
  assertEq(o[0].entries.length, 2);
  assertEq(o[0].bad, 0);
});

test("another agent's stale spool is never touched", () => {
  const d = mkdtempSync(join(dir, "o2b-"));
  const theirs = spoolFile(d, "opie", 7, 1);
  persistSpool(theirs, [entry("a", "1")]);
  ageFile(theirs, STALE_MS * 3);
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD }).length, 0);
  assert(existsSync(theirs), "file untouched");
});

test("agent match is exact: cc does not claim cc-foo's spool", () => {
  const d = mkdtempSync(join(dir, "o2c-"));
  const theirs = spoolFile(d, "cc-foo", 7, 1);
  persistSpool(theirs, [entry("a", "1")]);
  ageFile(theirs, STALE_MS * 3);
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD }).length, 0);
});

test("a stale spool whose pid is still alive is left alone (laptop slept)", () => {
  const d = mkdtempSync(join(dir, "o2d-"));
  const f = spoolFile(d, "cc", 4242, 1);
  persistSpool(f, [entry("a", "1")]);
  ageFile(f, STALE_MS * 3);
  const seen = [];
  const o = orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: (pid, t) => (seen.push([pid, t]), true) });
  assertEq(o.length, 0);
  assertEq(seen, [[4242, 1]], "asked about the pid and start time in the filename");
  assert(existsSync(f), "file untouched");
});

test("a second scan while the claimer lives finds nothing: the claim took the file", () => {
  const d = mkdtempSync(join(dir, "o2e-"));
  const f = spoolFile(d, "cc", 9, 1);
  persistSpool(f, [entry("a", "1")]);
  ageFile(f, STALE_MS * 3);
  const claimerAlive = (pid) => pid === process.pid;
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: claimerAlive }).length, 1);
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: claimerAlive }).length, 0);
  assertEq(readdirSync(d).length, 1);
  assert(readdirSync(d)[0].startsWith(`${basename(f)}.claimed-`), "the claimed file remains for the caller");
});

test("the real pid check: this process is alive, a wild pid is not", () => {
  assertEq(defaultPidAlive(process.pid, Date.now()), true);
  assertEq(defaultPidAlive(2 ** 22 - 7, Date.now()), false);
});

test("the real pid check sees a REUSED pid: alive number, but it started after the spool was recorded", () => {
  // Our own pid, "recorded" long before this process started → not the writer.
  const longBeforeThisProcess = Date.now() - 365 * 24 * 3600 * 1000;
  assertEq(defaultPidAlive(process.pid, longBeforeThisProcess), false);
});

test("a stranded claimed file whose claimer is gone is claimed again", () => {
  const d = mkdtempSync(join(dir, "o2g-"));
  const f = spoolFile(d, "cc", 9, 1);
  persistSpool(f, [entry("a", "1")]);
  const stranded = `${f}.claimed-77-5`;
  renameSync(f, stranded);
  ageFile(stranded, STALE_MS * 3);
  const asked = [];
  const o = orphanSpools(d, {
    agentId: "cc", ownFile: null, staleMs: STALE_MS, claimerPid: 4242,
    pidAlive: (pid, t) => (asked.push([pid, t]), false),
  });
  assertEq(asked, [[9, 1], [77, 5]], "writer and previous claimer both checked");
  assertEq(o.length, 1);
  assertEq(o[0].entries.length, 1);
  assert(o[0].file.startsWith(`${f}.claimed-4242-`), `re-claimed under the new claimer: ${o[0].file}`);
  assert(!existsSync(stranded), "old claim name gone");
});

test("a claimed file whose claimer is still alive is left alone", () => {
  const d = mkdtempSync(join(dir, "o2h-"));
  const f = spoolFile(d, "cc", 9, 1);
  persistSpool(f, [entry("a", "1")]);
  const busy = `${f}.claimed-${process.pid}-${Date.now()}`;
  renameSync(f, busy);
  ageFile(busy, STALE_MS * 3);
  const o = orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: (pid) => pid === process.pid });
  assertEq(o.length, 0);
  assert(existsSync(busy), "untouched");
});

test("one unreadable spool is skipped; the others are still returned", () => {
  const d = mkdtempSync(join(dir, "o2i-"));
  const bad = spoolFile(d, "cc", 1, 1);
  const good = spoolFile(d, "cc", 2, 2);
  persistSpool(bad, [entry("a", "1")]);
  persistSpool(good, [entry("b", "1")]);
  ageFile(bad, STALE_MS * 3);
  ageFile(good, STALE_MS * 2);
  chmodSync(bad, 0o000);
  let o;
  try {
    o = orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD });
  } finally {
    for (const n of readdirSync(d)) chmodSync(join(d, n), 0o600);
  }
  if (process.getuid && process.getuid() === 0) return; // root reads anything; nothing to prove
  assertEq(o.map((x) => x.entries[0].summary), ["1"]);
  assertEq(o[0].file.startsWith(`${good}.claimed-`), true);
});

test("own file is skipped even when stale", () => {
  const d = mkdtempSync(join(dir, "o3-"));
  const mine = spoolFile(d, "cc", 5, 1);
  persistSpool(mine, [entry("a", "1")]);
  ageFile(mine, STALE_MS * 3);
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: mine, staleMs: STALE_MS, pidAlive: DEAD }).length, 0);
});

test("non-spool files in the dir are ignored", () => {
  const d = mkdtempSync(join(dir, "o4-"));
  writeFileSync(join(d, "boot-cuts.jsonl"), '{"tool":"x","summary":"y"}\n');
  writeFileSync(join(d, `${SPOOL_PREFIX}cc-1-1.txt`), '{"tool":"x","summary":"y"}\n');
  for (const n of ["boot-cuts.jsonl", `${SPOOL_PREFIX}cc-1-1.txt`]) ageFile(join(d, n), STALE_MS * 2);
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD }).length, 0);
});

test("orphans come back oldest first", () => {
  const d = mkdtempSync(join(dir, "o5-"));
  const newer = spoolFile(d, "cc", 1, 1);
  const older = spoolFile(d, "cc", 2, 2);
  persistSpool(newer, [entry("n", "1")]);
  persistSpool(older, [entry("o", "1")]);
  ageFile(newer, STALE_MS + 60_000);
  ageFile(older, STALE_MS + 600_000);
  const o = orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, pidAlive: DEAD });
  assertEq(o.map((x) => x.file.replace(/\.claimed-.*$/, "")), [older, newer]);
});

test("staleness is judged against the caller's clock", () => {
  const d = mkdtempSync(join(dir, "o6-"));
  const f = spoolFile(d, "cc", 1, 1);
  persistSpool(f, [entry("a", "1")]);
  const future = Date.now() + STALE_MS + 1000;
  assertEq(orphanSpools(d, { agentId: "cc", ownFile: null, staleMs: STALE_MS, now: future, pidAlive: DEAD }).length, 1);
});

rmSync(dir, { recursive: true, force: true });

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed > 0 ? 1 : 0);
