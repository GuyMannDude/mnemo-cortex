// Tests for lane-candidates.js — which brain files are this agent's lane.
// Run: node lane-candidates.test.js
//
// Style matches lane-guard.test.js: homemade runner, plain console output.

import { laneCandidates, sessionPrefix } from "./lane-candidates.js";

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

// ── sessionPrefix ────────────────────────────────────────────────

test("sessionPrefix: no override = tenant only (CC, Rocky, Opie unchanged)", () => {
  assert(sessionPrefix("cc") === "cc", "cc default");
  assert(sessionPrefix("rocky", "") === "rocky", "empty override");
  assert(sessionPrefix("opie", "   ") === "opie", "whitespace-only override");
});

test("sessionPrefix: MNEMO_LANE adds the lane tag after the tenant (the CC3/CC2 snag)", () => {
  assert(sessionPrefix("cc", "cc3-igor.md") === "cc-cc3", "CC3");
  assert(sessionPrefix("cc", "cc2-igor2.md") === "cc-cc2", "CC2");
  assert(sessionPrefix("cc", " cc3-igor \n") === "cc-cc3", "trimmed, no .md");
  assert(sessionPrefix("cc", "scout.md") === "cc-scout", "lane name with no dash");
});

test("sessionPrefix: a lane named after the tenant adds no tag (no cc-cc-)", () => {
  assert(sessionPrefix("cc", "cc-session.md") === "cc", "cc-session.md");
  assert(sessionPrefix("cc", "cc.md") === "cc", "cc.md");
});

test("sessionPrefix: tag is kept to the server's session-id charset", () => {
  assert(sessionPrefix("cc", "c c3!-igor.md") === "cc-cc3", "junk stripped");
  assert(sessionPrefix("cc", "---.md") === "cc", "nothing left = no tag");
});

test("sessionPrefix: minted ids still satisfy every census consumer", () => {
  const sid = `${sessionPrefix("cc", "cc3-igor.md")}-2026-09-23-20-23-31`;
  // agentb/config.py _SESSION_ID_RE — the server refuses anything else.
  assert(/^[A-Za-z0-9_-]{1,128}$/.test(sid), `server charset: ${sid}`);
  // server.js inferAgent() — display label for chunks without agent_id.
  const m = `session:${sid}`.match(/^session:(.+?)-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}/);
  assert(m && m[1] === "cc-cc3", `inferAgent label: ${m && m[1]}`);
  // mnemo-dream.py _is_auto_capture() keys on "-auto-" in the id.
  assert(`${sessionPrefix("cc", "cc3-igor.md")}-auto-1790220217000`.includes("-auto-"), "dreamer auto marker");
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
