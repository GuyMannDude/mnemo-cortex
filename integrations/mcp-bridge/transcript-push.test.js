// Tests for transcript-push.js — turns as text → append-only archive file.
// Run: node transcript-push.test.js
//
// Style matches transcript-watch.test.js: homemade runner, fake fs.

import { UUID_RE, derivedUuid, pushSessionId, normalizeText, readExisting, overlapCount, buildLines, appendPush } from "./transcript-push.js";

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

// Fake fs: one file, in memory.
function fakeFs(initial = null) {
  const files = new Map();
  if (initial) files.set("/imports/x.jsonl", initial);
  return {
    files,
    dirs: [],
    existsSync: (p) => files.has(p),
    readFileSync: (p) => files.get(p),
    appendFileSync: (p, s) => files.set(p, (files.get(p) || "") + s),
    mkdirSync(p) { this.dirs.push(p); },
  };
}

const fixedNow = () => Date.parse("2026-10-20T20:00:00.000Z");

console.log("\n── ids ──\n");

test("pushSessionId is a valid lowercase UUID, stable for the same input", () => {
  const a = pushSessionId("opie", "opie-2026-10-20-13-33-02");
  const b = pushSessionId("opie", "opie-2026-10-20-13-33-02");
  assert(UUID_RE.test(a), `not a uuid: ${a}`);
  assert(a === a.toLowerCase(), "uuid must be lowercase");
  assert(a === b, "same input must give the same uuid");
  assert(a[14] === "5", "version nibble must be 5");
  assert("89ab".includes(a[19]), "variant nibble must be 8/9/a/b");
});

test("different agent or session → different uuid", () => {
  const a = pushSessionId("opie", "s1");
  assert(a !== pushSessionId("opie", "s2"), "session must change the uuid");
  assert(a !== pushSessionId("cc", "s1"), "agent must change the uuid");
});

test("derivedUuid namespace separates line ids from session ids", () => {
  assert(derivedUuid("a", "x") !== derivedUuid("b", "x"), "namespace must matter");
});

console.log("\n── lines ──\n");

test("buildLines: archive-shaped user/assistant lines with a parent chain", () => {
  const sid = pushSessionId("opie", "s");
  const lines = buildLines({ sessionUuid: sid, turns: [{ role: "user", text: "hi" }, { role: "assistant", text: "hello" }], now: fixedNow, version: "v", agentId: "opie" });
  assert(lines.length === 2, `expected 2 lines, got ${lines.length}`);
  const [u, a] = lines.map((l) => JSON.parse(l));
  assert(u.type === "user" && u.message.role === "user" && u.message.content[0].text === "hi", "user line shape");
  assert(a.type === "assistant" && a.message.content[0].text === "hello", "assistant line shape");
  assert(typeof a.message.id === "string", "assistant line needs message.id (the server counts assistant turns by it)");
  assert(u.parentUuid === null && a.parentUuid === u.uuid, "parent chain");
  assert(u.sessionId === sid && a.sessionId === sid, "sessionId on every line");
  assert(u.timestamp === "2026-10-20T20:00:00.000Z" && a.timestamp === "2026-10-20T20:00:00.001Z", "push-time timestamps, ordered");
  assert(u.version === "v" && u.cwd === "mnemo-transcript-push:opie", "version + cwd name the push path");
});

test("buildLines: title line only at the start of a file", () => {
  const sid = pushSessionId("opie", "s");
  const first = buildLines({ sessionUuid: sid, turns: [{ role: "user", text: "x" }], title: "Cars", now: fixedNow });
  assert(first.length === 2 && JSON.parse(first[0]).type === "ai-title" && JSON.parse(first[0]).aiTitle === "Cars", "title first");
  const later = buildLines({ sessionUuid: sid, turns: [{ role: "user", text: "x" }], title: "Cars", startIndex: 3, now: fixedNow });
  assert(later.length === 1, "no title mid-file");
});

test("line uuids are deterministic by position, continue across pushes", () => {
  const sid = pushSessionId("opie", "s");
  const a = buildLines({ sessionUuid: sid, turns: [{ role: "user", text: "1" }, { role: "user", text: "2" }], now: fixedNow });
  const b = buildLines({ sessionUuid: sid, turns: [{ role: "user", text: "2" }], startIndex: 1, now: fixedNow });
  assert(JSON.parse(a[1]).uuid === JSON.parse(b[0]).uuid, "same position → same line uuid");
});

console.log("\n── overlap ──\n");

const T = (role, text) => ({ role, text });

test("overlapCount: incremental re-send of the tail is skipped", () => {
  const existing = [T("user", "a"), T("assistant", "b"), T("user", "c")];
  assert(overlapCount(existing, [T("user", "c"), T("assistant", "d")]) === 1, "tail of 1");
  assert(overlapCount(existing, [T("assistant", "b"), T("user", "c"), T("assistant", "d")]) === 2, "tail of 2");
});

test("overlapCount: full replay of the chat so far is skipped entirely", () => {
  const existing = [T("user", "a"), T("assistant", "b")];
  assert(overlapCount(existing, [T("user", "a"), T("assistant", "b"), T("user", "c")]) === 2, "whole prefix");
});

test("overlapCount: a repeated 'ok' is NOT a duplicate (overlap, not a set)", () => {
  const existing = [T("user", "ok"), T("assistant", "fine")];
  assert(overlapCount(existing, [T("user", "ok")]) === 0, "'ok' after 'fine' is new");
});

test("overlapCount: a re-typed turn with drifted whitespace or curly quotes still matches", () => {
  const existing = [T("user", "He said \u201chi\u201d \u2014 twice"), T("assistant", "ok  then")];
  assert(overlapCount(existing, [T("assistant", " ok then\n")]) === 1, "whitespace drift");
  assert(overlapCount(existing, [T("user", 'He said "hi" - twice'), T("assistant", "ok then")]) === 2, "quote + dash drift");
  assert(normalizeText("a\t b\n") === "a b", "normalize collapses whitespace");
});

test("pushSessionId of a client uuid is a DIFFERENT uuid (push file never shadows the export)", () => {
  const client = "0d6be5ef-07ea-566a-ba7c-afc8d0ec9ed1";
  const k = pushSessionId("opie", client);
  assert(UUID_RE.test(k) && k !== client, "derived, not the client's own key");
});

test("overlapCount: role matters, empty sides give 0", () => {
  assert(overlapCount([T("user", "x")], [T("assistant", "x")]) === 0, "role mismatch");
  assert(overlapCount([], [T("user", "x")]) === 0, "empty existing");
  assert(overlapCount([T("user", "x")], []) === 0, "empty incoming");
});

console.log("\n── appendPush ──\n");

test("first push creates the file with title + turns, newline-terminated", () => {
  const fs = fakeFs();
  const sid = pushSessionId("opie", "s");
  const r = appendPush({ file: "/imports/x.jsonl", turns: [T("user", "hi"), T("assistant", "yo")], title: "Hi", sessionUuid: sid, now: fixedNow, version: "v", agentId: "opie", fs });
  assert(r.appended === 2 && r.skipped === 0 && r.total === 2, `result ${JSON.stringify(r)}`);
  const body = fs.files.get("/imports/x.jsonl");
  assert(body.endsWith("\n"), "newline-terminated");
  const lines = body.trim().split("\n").map((l) => JSON.parse(l));
  assert(lines.length === 3 && lines[0].type === "ai-title", "title + 2 turns");
  assert(fs.dirs.includes("/imports"), "mkdir of the folder");
});

test("second push appends only the new turns and chains the parent", () => {
  const fs = fakeFs();
  const sid = pushSessionId("opie", "s");
  const common = { file: "/imports/x.jsonl", sessionUuid: sid, now: fixedNow, version: "v", agentId: "opie", fs };
  appendPush({ ...common, turns: [T("user", "hi"), T("assistant", "yo")], title: "Hi" });
  const r = appendPush({ ...common, turns: [T("assistant", "yo"), T("user", "more"), T("assistant", "sure")], title: "Hi" });
  assert(r.appended === 2 && r.skipped === 1 && r.total === 4, `result ${JSON.stringify(r)}`);
  const lines = fs.files.get("/imports/x.jsonl").trim().split("\n").map((l) => JSON.parse(l));
  assert(lines.length === 5, "title + 4 turns");
  assert(lines.filter((l) => l.type === "ai-title").length === 1, "title once");
  assert(lines[3].parentUuid === lines[2].uuid, "parent chain continues across pushes");
  const { turns } = readExisting(fs.files.get("/imports/x.jsonl"));
  assert(turns.length === 4 && turns[3].text === "sure", "readExisting sees all four");
});

test("a push with nothing new writes nothing", () => {
  const fs = fakeFs();
  const sid = pushSessionId("opie", "s");
  const common = { file: "/imports/x.jsonl", sessionUuid: sid, now: fixedNow, fs };
  appendPush({ ...common, turns: [T("user", "hi")] });
  const before = fs.files.get("/imports/x.jsonl");
  const r = appendPush({ ...common, turns: [T("user", "hi")] });
  assert(r.appended === 0 && r.skipped === 1 && r.total === 1, `result ${JSON.stringify(r)}`);
  assert(fs.files.get("/imports/x.jsonl") === before, "file unchanged");
});

test("a prior file without a final newline is not glued", () => {
  const fs = fakeFs('{"type":"user","uuid":"u0","sessionId":"x","message":{"role":"user","content":[{"type":"text","text":"old"}]}}');
  const sid = pushSessionId("opie", "s");
  const r = appendPush({ file: "/imports/x.jsonl", turns: [T("assistant", "new")], sessionUuid: sid, now: fixedNow, fs });
  assert(r.appended === 1 && r.total === 2, `result ${JSON.stringify(r)}`);
  const lines = fs.files.get("/imports/x.jsonl").trim().split("\n");
  assert(lines.length === 2, `two lines, got ${lines.length}`);
  assert(JSON.parse(lines[1]).parentUuid === "u0", "parent = the old last uuid");
});

test("readExisting ignores junk lines and other types", () => {
  const { turns, lastUuid } = readExisting('not json\n{"type":"ai-title","aiTitle":"t"}\n{"type":"user","uuid":"a","message":{"role":"user","content":[{"type":"text","text":"q"}]}}\n\n');
  assert(turns.length === 1 && turns[0].text === "q" && lastUuid === "a", "one turn, last uuid a");
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
