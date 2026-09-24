// Tests for write-guard.js — compare-and-write for write_brain_file.
// Run: node write-guard.test.js
//
// Style matches write-budget.test.js: homemade runner, exit 1 on failure.

import { contentHash, recordSeen, resetSeen, isCurrent, staleWriteRefusal } from "./write-guard.js";

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

console.log("\n── write-guard ──\n");

test("new file (nothing on disk) passes without a read", () => {
  resetSeen();
  assert(staleWriteRefusal({ filename: "new.md", onDisk: null }) === null, "null onDisk");
  assert(staleWriteRefusal({ filename: "new.md", onDisk: undefined }) === null, "undefined onDisk");
});

test("existing file never read this session is refused, names read_brain_file", () => {
  resetSeen();
  const r = staleWriteRefusal({ filename: "opie.md", onDisk: "# Opie\n" });
  assert(r && r.startsWith("Refused:"), `got: ${r}`);
  assert(r.includes('read_brain_file("opie.md")'), "should tell the agent how to recover");
  assert(r.includes("never read it"), "should say why");
});

test("read then unchanged write passes", () => {
  resetSeen();
  recordSeen("opie.md", "# Opie\nv1\n");
  assert(staleWriteRefusal({ filename: "opie.md", onDisk: "# Opie\nv1\n" }) === null);
});

test("read, then someone else changed the disk copy: refused", () => {
  resetSeen();
  recordSeen("guy-genealogy.md", "v1 with Arnold and Steward\n");
  const r = staleWriteRefusal({
    filename: "guy-genealogy.md",
    onDisk: "v2 written by the other session\n",
    lastCommit: "83d032f opie 2026-09-22 brain: opie updated guy-genealogy.md",
  });
  assert(r && r.includes("changed since this session last read it"), `got: ${r}`);
  assert(r.includes("83d032f"), "refusal carries the last-commit context");
  assert(r.includes("do not resend the old version"), "tells the agent to merge, not retry");
});

test("the session's own write re-records: a follow-up write passes", () => {
  resetSeen();
  recordSeen("cc-session.md", "boot copy\n");
  recordSeen("cc-session.md", "my rewrite\n"); // what write_brain_file does after landing
  assert(staleWriteRefusal({ filename: "cc-session.md", onDisk: "my rewrite\n" }) === null);
});

test("hashes are per-file: reading A does not license writing B", () => {
  resetSeen();
  recordSeen("a.md", "same\n");
  const r = staleWriteRefusal({ filename: "b.md", onDisk: "same\n" });
  assert(r && r.includes("never read it"), `got: ${r}`);
});

test("isCurrent: true only for the exact content last seen", () => {
  resetSeen();
  assert(!isCurrent("lane.md", "v1\n"), "nothing seen yet");
  recordSeen("lane.md", "v1\n");
  assert(isCurrent("lane.md", "v1\n"), "seen copy");
  assert(!isCurrent("lane.md", "v2 by someone else\n"), "changed copy");
});

test("contentHash is stable and byte-sensitive (CRLF vs LF differ)", () => {
  assert(contentHash("x\n") === contentHash("x\n"));
  assert(contentHash("x\n") !== contentHash("x\r\n"));
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
