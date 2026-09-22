// Tests for lane-candidates.js — which brain files are this agent's lane.
// Run: node lane-candidates.test.js
//
// Style matches lane-guard.test.js: homemade runner, plain console output.

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
  if (!cond) throw new Error(msg);
}

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

test("default: tenant-named lane, both spellings, in that order", () => {
  assert(same(laneCandidates("cc"), ["cc.md", "cc-session.md"]), "cc default");
  assert(same(laneCandidates("opie", ""), ["opie.md", "opie-session.md"]), "empty override = default");
  assert(same(laneCandidates("rocky", undefined), ["rocky.md", "rocky-session.md"]), "undefined override = default");
});

test("MNEMO_LANE override is the ONLY candidate (the CC2-on-tenant-cc case)", () => {
  assert(same(laneCandidates("cc", "cc2-igor2.md"), ["cc2-igor2.md"]), "override wins, tenant lane dropped");
});

test("override without .md gets the extension", () => {
  assert(same(laneCandidates("cc", "cc2-igor2"), ["cc2-igor2.md"]), "extension appended");
});

test("override is trimmed (env files carry stray whitespace)", () => {
  assert(same(laneCandidates("cc", "  cc2-igor2.md \n"), ["cc2-igor2.md"]), "trimmed");
  assert(same(laneCandidates("cc", "   "), ["cc.md", "cc-session.md"]), "whitespace-only = default");
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
