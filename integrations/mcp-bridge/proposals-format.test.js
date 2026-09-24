// Tests for proposals-format.js — mnemo_fact_proposals' pure parts (2.33.0).
// No Mnemo server needed. Run: node proposals-format.test.js
//
// Style matches transcript-format.test.js: homemade runner, plain output.

import { listPath, resolvePath, formatList, formatResolve } from "./proposals-format.js";

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

function eq(a, b, msg) {
  if (a !== b) throw new Error(`${msg || "not equal"}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`);
}

function ok(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}

const FACT_ROW = {
  id: 3, status: "pending", entity: "igor-2", attribute: "role", authority: "declared",
  created_at: 1790200000, seen_count: 2, last_seen: 1790210000, source_agent: "cc",
  proposed_value: "x", current_value: "y", evidence_source: "dreamer",
  // 4.26.0 envelope fields ride along on fact rows; the fact view ignores them
  target_kind: "fact", evidence_quotes: ["dreamer"], confidence_score: null,
};

// The 2.32.0 output for the same row, written out by hand from the 2.32.0
// formatter — the no-arg call must print exactly this (acceptance 8).
const GOLDEN_232 = [
  "1 pending proposal(s):",
  "  #3 x2 [pending] igor-2.role (declared) 2026-09-24T00:33 by cc",
  "      proposed: x",
  "      current:  y",
  "      evidence: dreamer",
].join("\n");

console.log("proposals-format");

test("no-arg list keeps the 2.32.0 route", () => {
  eq(listPath({}), "/facts/proposals");
  eq(listPath({ status: "all", limit: 5 }), "/facts/proposals?status=all&limit=5");
  eq(listPath({ kind: "fact", status: "pending" }), "/facts/proposals?status=pending");
});

test("no-arg list keeps the 2.32.0 text, byte for byte", () => {
  eq(formatList({ proposals: [FACT_ROW], count: 1, status: "pending" }), GOLDEN_232);
  eq(formatList({ proposals: [], count: 0, status: "pending" }), "No pending proposals.");
});

test("other kinds read /proposals?kind=", () => {
  eq(listPath({ kind: "memory_category" }), "/proposals?kind=memory_category");
  eq(listPath({ kind: "all", status: "pending", limit: 10 }), "/proposals?status=pending&limit=10&kind=all");
});

test("memory rows show target, score and quotes", () => {
  const text = formatList({
    kind: "memory_category", status: "pending", count: 1, proposals: [{
      id: 7, status: "pending", target_kind: "memory_category", target_ref: "memory:cc/abc",
      created_at: 1790200000, seen_count: 1, source_run: "jev:shadow:v1", confidence_score: 0.93,
      proposed_value: "decision", current_value: "topology", evidence_quotes: ["We chose SQLite"],
    }],
  });
  ok(text.startsWith("1 pending memory_category proposal(s):"), text);
  ok(text.includes("#7 [pending] memory_category memory:cc/abc"), text);
  ok(text.includes("by jev:shadow:v1 confidence 0.93"), text);
  ok(text.includes("quote:    We chose SQLite"), text);
  eq(formatList({ kind: "memory_note", status: "pending", count: 0, proposals: [] }),
     "No pending memory_note proposals.");
});

test("resolve uses the route every server version has", () => {
  eq(resolvePath(12), "/facts/proposals/12/resolve");
});

test("resolve text: fact vs memory", () => {
  eq(formatResolve(3, { reason: "proposal #3 accepted", written: true, previous_value: "y",
                        previous_confidence: "high_probability", authority: "declared" }),
     "Proposal #3: proposal #3 accepted\n  slot now = proposed value [verified], was: y [high_probability]; tier stays declared");
  ok(formatResolve(7, { reason: "proposal #7 accepted", written: true, previous_value: "topology",
                        authority: "n/a" }).includes("memory category now = proposed value, was: topology"));
  eq(formatResolve(8, { reason: "kind not promotable yet", written: false }),
     "Proposal #8: kind not promotable yet");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
