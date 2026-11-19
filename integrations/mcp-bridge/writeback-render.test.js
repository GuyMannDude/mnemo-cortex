// Tests for writeback-render.js — one render for every writer tool (2.42.0,
// item 18a). No Mnemo server needed. Run: node writeback-render.test.js
//
// Style matches proposals-format.test.js: homemade runner, plain output.
// The last block reads server.js itself (path overridable with SERVER_JS, for
// a fail-before run against the old source).

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  STORED_STATUS, PAUSE_GATE_RULE, writebackVerdict, mnemoSaveLine, flushNotice,
} from "./writeback-render.js";

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
function eq(a, b) { if (a !== b) throw new Error(`expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
function ok(c, msg) { if (!c) throw new Error(msg || "assertion failed"); }
function has(s, sub) { ok(s.includes(sub), `${JSON.stringify(s)} does not contain ${JSON.stringify(sub)}`); }
function lacks(s, sub) { ok(!s.includes(sub), `${JSON.stringify(s.length > 200 ? s.slice(0, 200) + "…" : s)} contains ${JSON.stringify(sub)}`); }

console.log("\n── writebackVerdict ──\n");

test("archived is the only stored /writeback status", () => {
  const v = writebackVerdict({ status: "archived", memory_id: "abc123" }, { agent: "cc" });
  eq(v.stored, true); eq(v.isError, false); eq(v.id, "abc123");
  has(v.line, "Saved"); has(v.line, "abc123");
});

test("saved is the only stored /trajectory/save status", () => {
  const v = writebackVerdict({ status: "saved", trajectory_id: "t1" }, { route: "trajectory" });
  eq(v.stored, true); eq(v.id, "t1");
  // and archived is NOT a stored trajectory reply
  eq(writebackVerdict({ status: "archived", trajectory_id: "t1" }, { route: "trajectory" }).stored, false);
  // nor saved a stored writeback reply
  eq(writebackVerdict({ status: "saved", memory_id: "m" }).stored, false);
});

test("paused: NOT SAVED, isError, names agent + gate rule + raw status", () => {
  const v = writebackVerdict({ status: "paused", memory_id: "", message: "Capture is paused" }, { agent: "cc" });
  eq(v.stored, false); eq(v.isError, true); eq(v.id, "");
  has(v.line, "NOT SAVED"); has(v.line, "status=paused"); has(v.line, "for cc");
  has(v.line, PAUSE_GATE_RULE); has(v.line, "Server: Capture is paused");
  lacks(v.line, "Saved (");
});

test("paused on the trajectory route does not quote the /writeback gate rule", () => {
  const v = writebackVerdict({ status: "paused" }, { route: "trajectory", agent: "cc" });
  eq(v.stored, false); eq(v.isError, true); has(v.line, "NOT SAVED"); lacks(v.line, PAUSE_GATE_RULE);
});

test("superseded: NOT STORED, isError, names the id and the prior, says it once", () => {
  const v = writebackVerdict({ status: "superseded", memory_id: "m9", superseded_by: "m3",
    message: "Memory m9 was superseded (m3); this re-send was not stored." });
  eq(v.stored, false); eq(v.isError, true);
  has(v.line, "NOT STORED"); has(v.line, "m9"); has(v.line, "by m3"); has(v.line, "status=superseded");
  eq(v.line.split("was not stored").length, 2);
});

test("held: NOT SAVED but not an error (a review step)", () => {
  const v = writebackVerdict({ status: "held", near_duplicates: [{ id: "x" }] });
  eq(v.stored, false); eq(v.isError, false); has(v.line, "NOT SAVED"); has(v.line, "status=held");
});

test("an unknown status fails closed with the raw status", () => {
  const v = writebackVerdict({ status: "queued", memory_id: "m1" });
  eq(v.stored, false); eq(v.isError, true); eq(v.id, "");
  has(v.line, "NOT SAVED"); has(v.line, "status=queued");
});

test("a missing status, an empty body and a non-object all fail closed", () => {
  for (const [data, raw] of [[{ memory_id: "m1" }, "(none)"], [{ status: "" }, "(none)"],
                             [null, "(no reply body)"], [undefined, "(no reply body)"], ["ok", "(no reply body)"]]) {
    const v = writebackVerdict(data);
    eq(v.stored, false); eq(v.isError, true); has(v.line, `status=${raw}`);
  }
});

test("stored statuses are frozen", () => {
  eq(STORED_STATUS.writeback, "archived"); eq(STORED_STATUS.trajectory, "saved");
  ok(Object.isFrozen(STORED_STATUS));
});

console.log("\n── mnemoSaveLine (session_checkpoint / session_end) ──\n");

test("stored: OK with id and session", () => {
  const s = mnemoSaveLine({ status: "archived", memory_id: "m1" }, { sid: "cc-1" });
  eq(s.stored, true); eq(s.text, "Mnemo save: OK (memory_id=m1, session cc-1)");
});

test("stored without a session id: no session clause (session_end shape)", () => {
  eq(mnemoSaveLine({ status: "archived", memory_id: "m1" }).text, "Mnemo save: OK (memory_id=m1)");
});

test("held keeps the caller's hint and the duplicate ids", () => {
  const s = mnemoSaveLine({ status: "held", near_duplicates: [{ id: "a" }, { id: "b" }] }, { heldHint: "Repeat with force=true." });
  eq(s.text, "Mnemo save: HELD as near-duplicate of a, b — NOT saved. Repeat with force=true.");
});

test("paused / superseded / unknown never say OK", () => {
  for (const data of [{ status: "paused" }, { status: "superseded", memory_id: "m1", superseded_by: "m0" },
                      { status: "mystery", memory_id: "m1" }, {}]) {
    const s = mnemoSaveLine(data, { agent: "cc", sid: "cc-1" });
    eq(s.stored, false);
    lacks(s.text, "OK"); ok(/NOT (SAVED|STORED)/.test(s.text), s.text);
  }
});

console.log("\n── flushNotice (auto-capture) ──\n");

test("a stored flush says nothing", () => {
  eq(flushNotice({ status: "archived", memory_id: "m" }, 5, "cc"), null);
});

test("a paused flush says how many entries the pause discarded", () => {
  eq(flushNotice({ status: "paused" }, 7, "cc"),
     "[auto-capture] 7 entries discarded by capture pause (agent cc, status=paused)");
});

test("any other status says NOT stored with the raw status", () => {
  eq(flushNotice({ status: "held" }, 2, "cc"), "[auto-capture] 2 entries NOT stored (agent cc, status=held)");
  eq(flushNotice(undefined, 1, ""), "[auto-capture] 1 entries NOT stored (agent default, status=(no reply body))");
});

console.log("\n── server.js census: every writer renders through the helper ──\n");

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(process.env.SERVER_JS || join(here, "server.js"), "utf8");

test("no writer falls back to the literal \"ok\" as an id", () => {
  for (const pat of [/memory_id\s*\|\|\s*"ok"/, /trajectory_id\s*\|\|\s*"ok"/, /memoryId\s*\|\|\s*"ok"/]) {
    ok(!pat.test(src), `server.js still matches ${pat}`);
  }
});

test("each writer uses the helper (mnemo_save, trajectory, checkpoint, session_end, flush)", () => {
  const uses = (src.match(/\b(writebackVerdict|mnemoSaveLine|flushNotice)\(/g) || []).length;
  ok(uses >= 5, `expected >= 5 helper calls, found ${uses}`);
  ok(/flushNotice\(/.test(src), "sendCaptureBatch does not call flushNotice");
  ok(/route:\s*"trajectory"/.test(src), "mnemo_save_trajectory does not use the trajectory route");
});

// The body of one registered tool: from its registerTool name to the next.
function toolBody(name) {
  const start = src.indexOf(`server.registerTool(\n  "${name}",`);
  ok(start >= 0, `tool ${name} not found in server.js`);
  const next = src.indexOf("server.registerTool(", start + 1);
  return src.slice(start, next < 0 ? undefined : next);
}

test("each named writer renders through the helper in its own body", () => {
  ok(/writebackVerdict\(data, \{ agent: AGENT_ID \}\)/.test(toolBody("mnemo_save")), "mnemo_save");
  ok(/writebackVerdict\(data, \{ route: "trajectory"/.test(toolBody("mnemo_save_trajectory")), "mnemo_save_trajectory");
  ok(/mnemoSaveLine\(data,/.test(toolBody("session_checkpoint")), "session_checkpoint");
  ok(/mnemoSaveLine\(data,/.test(toolBody("session_end")), "session_end");
  const flush = src.slice(src.indexOf("async function sendCaptureBatch"), src.indexOf("const capture = createCaptureBuffer"));
  ok(/flushNotice\(data,/.test(flush), "sendCaptureBatch");
});

test("a checkpoint id is kept for the force retry only when the save was stored", () => {
  const pushes = src.match(/checkpointIds\.push\(/g) || [];
  eq(pushes.length, 1);
  ok(/if \(save\.stored\) \{\s*memoryId = save\.id \|\| null;\s*if \(memoryId\) checkpointIds\.push\(memoryId\);\s*\}/
       .test(toolBody("session_checkpoint")), "checkpointIds.push is not guarded by save.stored");
});

test("the pause tool no longer promises every manual save lands", () => {
  lacks(src, "Manual mnemo_save still works");
  lacks(src, "Deliberate mnemo_save calls still work");
  lacks(src, "other agents keep capturing");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
