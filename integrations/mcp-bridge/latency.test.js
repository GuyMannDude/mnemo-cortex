// Tests for latency.js — the slim always-on tool-call timing log.
// Run: node latency.test.js
//
// Style matches dump.test.js: homemade runner against a tmpdir,
// plain console output, exit 1 on any failure.

import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LatencyWriter, resolveLatencyConfig } from "./latency.js";

let passed = 0;
let failed = 0;

async function test(name, fn) {
  try {
    await fn();
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

function freshDir() {
  return mkdtempSync(join(tmpdir(), "mnemo-latency-test-"));
}

function readLines(dir, agent) {
  const date = new Date().toISOString().slice(0, 10);
  const path = join(dir, agent, `${date}.jsonl`);
  if (!existsSync(path)) return null;
  return readFileSync(path, "utf8")
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l))
    .filter((e) => e.kind !== "header");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await test("resolveLatencyConfig: on by default, off only on 'off'", () => {
  assert(resolveLatencyConfig({ HOME: "/h" }).enabled === true, "default should be on");
  assert(resolveLatencyConfig({ HOME: "/h", MNEMO_LATENCY: "OFF" }).enabled === false, "off should disable");
  assert(resolveLatencyConfig({ HOME: "/h", MNEMO_LATENCY: "on" }).enabled === true, "on should enable");
  const c = resolveLatencyConfig({ HOME: "/h", MNEMO_LATENCY_DIR: "/elsewhere" });
  assert(c.dir === "/elsewhere", "dir override ignored");
});

await test("disabled: returns the handler untouched, writes nothing", async () => {
  const dir = freshDir();
  const w = new LatencyWriter("cc", { enabled: false, dir, retentionDays: 0 });
  const handler = async () => ({ content: [] });
  assert(w.wrap("mnemo_recall", handler) === handler, "should be the same reference");
  await w.wrap("mnemo_recall", handler)();
  assert(readLines(dir, "cc") === null, "no file should exist");
  rmSync(dir, { recursive: true, force: true });
});

await test("success: one line with tool, ms >= elapsed, ok:true", async () => {
  const dir = freshDir();
  const w = new LatencyWriter("cc", { enabled: true, dir, retentionDays: 0 });
  const wrapped = w.wrap("mnemo_recall", async () => {
    await sleep(25);
    return { content: [{ type: "text", text: "hi" }] };
  });
  const res = await wrapped({ query: "x" });
  assert(res.content[0].text === "hi", "response must pass through unchanged");
  const lines = readLines(dir, "cc");
  assert(lines.length === 1, `expected 1 line, got ${lines.length}`);
  const e = lines[0];
  assert(e.kind === "latency", "kind should be latency");
  assert(e.tool === "mnemo_recall", "tool name recorded");
  assert(e.ok === true, "ok should be true");
  assert(typeof e.ms === "number" && e.ms >= 20, `ms should reflect elapsed, got ${e.ms}`);
  assert(e.params === undefined && e.response === undefined, "slim line: no params or response");
  rmSync(dir, { recursive: true, force: true });
});

await test("isError response: recorded ok:false, response still returned", async () => {
  const dir = freshDir();
  const w = new LatencyWriter("cc", { enabled: true, dir, retentionDays: 0 });
  const wrapped = w.wrap("mnemo_save", async () => ({
    content: [{ type: "text", text: "Save error: boom" }],
    isError: true,
  }));
  const res = await wrapped({});
  assert(res.isError === true, "error response must pass through");
  const lines = readLines(dir, "cc");
  assert(lines.length === 1 && lines[0].ok === false, "isError must record ok:false");
  rmSync(dir, { recursive: true, force: true });
});

await test("thrown error: recorded ok:false, error rethrown", async () => {
  const dir = freshDir();
  const w = new LatencyWriter("cc", { enabled: true, dir, retentionDays: 0 });
  const wrapped = w.wrap("mnemo_save", async () => {
    throw new Error("kaboom");
  });
  let threw = false;
  try {
    await wrapped({});
  } catch (err) {
    threw = err.message === "kaboom";
  }
  assert(threw, "original error must be rethrown");
  const lines = readLines(dir, "cc");
  assert(lines.length === 1 && lines[0].ok === false, "throw must record ok:false");
  rmSync(dir, { recursive: true, force: true });
});

await test("multiple calls accumulate lines in one day file", async () => {
  const dir = freshDir();
  const w = new LatencyWriter("cc", { enabled: true, dir, retentionDays: 0 });
  const wrapped = w.wrap("mnemo_recall", async () => ({ content: [] }));
  await wrapped({});
  await wrapped({});
  await wrapped({});
  const lines = readLines(dir, "cc");
  assert(lines.length === 3, `expected 3 lines, got ${lines.length}`);
  rmSync(dir, { recursive: true, force: true });
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
