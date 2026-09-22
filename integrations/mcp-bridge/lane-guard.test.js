// Tests for lane-guard.js — write_brain_file's protected-file check.
// Run: node lane-guard.test.js
//
// Style matches boot-budget.test.js: homemade runner, plain console output.

import { refusesBrainWrite } from "./lane-guard.js";
import { laneCandidates } from "./lane-candidates.js";

// Second argument is the agent's lane-candidate list, as server.js passes it.
const lanes = (agent, override) => laneCandidates(agent, override);

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

test("owner may write its own -session lane (the v2.18.0 lockout bug)", () => {
  assert(!refusesBrainWrite("cc-session.md", lanes("cc")), "cc must be allowed to write cc-session.md");
});

test("other agents stay refused from a protected lane", () => {
  assert(refusesBrainWrite("cc-session.md", lanes("opie")), "opie must be refused");
  assert(refusesBrainWrite("cc-session.md", lanes("rocky")), "rocky must be refused");
  assert(refusesBrainWrite("cc-session.md", lanes("openclaw")), "default agent must be refused");
});

test("CLAUDE.md refused for everyone (matches no lane pattern)", () => {
  assert(refusesBrainWrite("CLAUDE.md", lanes("cc")), "cc must be refused");
  assert(refusesBrainWrite("CLAUDE.md", lanes("opie")), "opie must be refused");
});

test("agent literally named CLAUDE still cannot claim CLAUDE.md", () => {
  // Lane candidates are CLAUDE.md-shaped only via `${agent}.md`; guard
  // against a spoofy MNEMO_AGENT_ID=CLAUDE unlocking the operating doc.
  assert(refusesBrainWrite("CLAUDE.md", lanes("CLAUDE")), "MNEMO_AGENT_ID=CLAUDE must not unlock CLAUDE.md");
});

test("MNEMO_LANE override: CC2 as tenant cc owns cc2-igor2.md, NOT cc-session.md", () => {
  assert(refusesBrainWrite("cc-session.md", lanes("cc", "cc2-igor2.md")), "CC2 must be refused CC's lane");
  assert(!refusesBrainWrite("cc2-igor2.md", lanes("cc", "cc2-igor2.md")), "CC2 writes its own lane");
});

test("unprotected files pass for any agent", () => {
  assert(!refusesBrainWrite("active.md", lanes("opie")), "active.md is joint-write");
  assert(!refusesBrainWrite("incidents.md", lanes("cc")), "incidents.md is joint-write");
  assert(!refusesBrainWrite("opie.md", lanes("opie")), "opie writes opie.md");
  assert(!refusesBrainWrite("dave-session.md", lanes("dave")), "dave writes dave-session.md");
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
