// Tests for transcript-format.js — mnemo_transcript's pure parts.
// No Mnemo server needed. Run: node transcript-format.test.js
//
// Style matches checkpoint.test.js: homemade runner, plain console output.

import {
  SESSION_KEY_RE, TURNS_PAGE, searchPath, getPath,
  formatSearch, formatManifest, formatTurns,
} from "./transcript-format.js";

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

const SID = "0f8fad5b-d9cb-469f-a165-70867728950e";

console.log("\ntranscript-format\n");

test("session key accepts a UUID and a subagent key, rejects paths", () => {
  ok(SESSION_KEY_RE.test(SID));
  ok(SESSION_KEY_RE.test(`${SID}_agent-a3181710c8d1f6bbc`));
  ok(!SESSION_KEY_RE.test("../etc/passwd"));
  ok(!SESSION_KEY_RE.test(`${SID}/../x`));
});

test("search path carries agent, q and limit; list omits q", () => {
  eq(searchPath("cc", "zebra widget", 5), "/transcripts?agent_id=cc&q=zebra+widget&limit=5");
  eq(searchPath("cc"), "/transcripts?agent_id=cc");
});

test("get path: manifest/raw use format, turns page defaults to TURNS_PAGE", () => {
  eq(getPath("cc", SID, "manifest"), `/transcripts/${SID}?agent_id=cc&format=manifest`);
  eq(getPath("cc", SID, "turns"), `/transcripts/${SID}/turns?agent_id=cc&from=0&limit=${TURNS_PAGE}`);
  eq(getPath("cc", SID, "turns", 10, 14), `/transcripts/${SID}/turns?agent_id=cc&from=10&to=14&limit=5`);
});

test("search formats hits with seq and a read hint; empty says so", () => {
  const t = formatSearch({ q: "zebra", results: [
    { session_id: SID, seq: 3, ts: "T", role: "user", kind: "text", snippet: "[zebra] x" }] });
  ok(t.includes(`${SID} #3 [T] user/text`));
  ok(t.includes("format=turns from=<seq>"));
  ok(formatSearch({ q: "none", results: [] }).includes("No archived turn matches"));
  ok(formatSearch({ sessions: [] }).includes("No transcripts archived"));
});

test("manifest names subagent parent and redaction count", () => {
  const t = formatManifest({ session_id: `${SID}_agent-a1`, parent_session_id: SID, agent_id: "cc",
    user_turns: 1, assistant_turns: 2, tool_use: 3, indexed_turns: 9, bytes: 10, lines: 4,
    bad_lines: 0, redactions_applied: 2, uploads: 1, sha256: "abc" });
  ok(t.includes(`subagent of: ${SID}`));
  ok(t.includes("redactions: 2"));
});

test("turns page stops at the budget and names the next from", () => {
  const turns = [0, 1, 2].map((seq) => ({ seq, ts: "T", role: "user", kind: "text", text: "x".repeat(40) }));
  const { text, nextFrom } = formatTurns({ session_id: SID, turns, next_from: null }, 120);
  eq(nextFrom, 2);
  ok(text.includes("#1 [T]") && !text.includes("#2 [T]"));
  ok(text.endsWith("[more: call again with from=2]"));
});

test("a turn larger than the whole budget is cut, loudly", () => {
  const turns = [{ seq: 7, ts: "T", role: "user", kind: "tool_result", tool_name: "Read", text: "y".repeat(500) }];
  const { text, nextFrom } = formatTurns({ session_id: SID, turns }, 100);
  eq(nextFrom, 8);
  ok(text.includes("turn #7 cut at 100 of 500 chars"));
  ok(text.includes("user/tool_result(Read)"));
});

test("server's own next_from survives when the page fits", () => {
  const turns = [{ seq: 0, ts: "T", role: "assistant", kind: "text", text: "hi" }];
  eq(formatTurns({ session_id: SID, turns, next_from: 1 }).nextFrom, 1);
  eq(formatTurns({ session_id: SID, turns, next_from: null }).nextFrom, null);
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
