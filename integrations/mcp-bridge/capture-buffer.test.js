// Tests for capture-buffer.js — the auto-capture state machine against a
// stubbed sender and an in-memory spool. No Mnemo server needed.
// Run: node capture-buffer.test.js
//
// Style matches checkpoint.test.js: homemade runner, plain console output.

import { createCaptureBuffer } from "./capture-buffer.js";

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

function assertEq(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg || "mismatch"}: got ${a}, want ${e}`);
}

const tick = () => new Promise((r) => setImmediate(r));
const entry = (tool, n) => ({ tool, summary: `${tool} ${n}`, ts: new Date().toISOString() });

// A sender whose completion the test controls, one call at a time.
function controlledSender() {
  const calls = [];
  const send = (batch) =>
    new Promise((resolve, reject) => {
      calls.push({ batch: batch.slice(), resolve, reject });
    });
  return { send, calls };
}

// Timers that never fire on their own; the test drives them.
function manualTimers() {
  const pending = new Map();
  let nextId = 1;
  return {
    setTimer: (fn, ms) => {
      const id = nextId++;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimer: (id) => pending.delete(id),
    pending,
    fireAll() {
      const fns = [...pending.values()].map((p) => p.fn);
      pending.clear();
      fns.forEach((fn) => fn());
    },
  };
}

function harness(opts = {}) {
  const sender = controlledSender();
  const timers = manualTimers();
  const spoolWrites = [];
  const logs = [];
  const buf = createCaptureBuffer({
    send: sender.send,
    spool: { persist: (entries) => spoolWrites.push(entries.map((e) => e.summary)) },
    log: (l) => logs.push(l),
    flushSize: 3,
    idleMs: 1000,
    maxBacklog: 5,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    ...opts,
  });
  return { buf, sender, timers, spoolWrites, logs, lastSpool: () => spoolWrites[spoolWrites.length - 1] };
}

console.log("\n── capture + spool ──\n");

await test("every capture rewrites the spool with the whole buffer", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  h.buf.capture(entry("b", 2));
  assertEq(h.lastSpool(), ["a 1", "b 2"]);
  assertEq(h.buf.size, 2);
});

await test("a spool failure is logged, not thrown into the capture", async () => {
  const h = harness({ spool: { persist: () => { throw new Error("disk full"); } } });
  h.buf.capture(entry("a", 1));
  assertEq(h.buf.size, 1);
  assert(h.logs.some((l) => l.includes("spool write failed") && l.includes("disk full")), "log line missing");
});

await test("the idle timer is re-armed on each capture", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  h.buf.capture(entry("a", 2));
  assertEq(h.timers.pending.size, 1, "one live timer");
});

console.log("\n── flush semantics ──\n");

await test("entries stay in buffer and spool until the send succeeds", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  const p = h.buf.flush();
  assertEq(h.buf.size, 1, "still buffered during flight");
  assertEq(h.lastSpool(), ["a 1"], "still spooled during flight");
  h.sender.calls[0].resolve();
  assertEq(await p, true);
  assertEq(h.buf.size, 0);
  assertEq(h.lastSpool(), [], "spool emptied after success");
  assert(h.logs.some((l) => l === "[auto-capture] flushed 1 entries"), "success log line");
});

await test("size trigger sends when the buffer reaches flushSize", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  h.buf.capture(entry("a", 2));
  assertEq(h.sender.calls.length, 0);
  h.buf.capture(entry("a", 3));
  assertEq(h.sender.calls.length, 1);
  assertEq(h.sender.calls[0].batch.length, 3);
});

await test("entries captured during a flight survive it", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  const p = h.buf.flush();
  h.buf.capture(entry("b", 2));
  h.sender.calls[0].resolve();
  await p;
  assertEq(h.buf.entries().map((e) => e.summary), ["b 2"]);
  assertEq(h.lastSpool(), ["b 2"]);
});

await test("a failed send keeps everything, rewrites the spool and schedules a retry", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  h.timers.pending.clear(); // drop the idle timer so we can see the retry get armed
  const p = h.buf.flush();
  h.sender.calls[0].reject(new Error("no route"));
  assertEq(await p, false);
  assertEq(h.buf.size, 1);
  assertEq(h.lastSpool(), ["a 1"]);
  assertEq(h.timers.pending.size, 1, "retry timer armed");
  assert(h.logs.some((l) => l.includes("flush failed") && l.includes("no route")), "failure log line");
  h.timers.fireAll();
  assertEq(h.sender.calls.length, 2, "retry sent");
});

await test("an idle timer that fires into a flight does not poison the retry", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  h.timers.pending.clear();
  const p = h.buf.flush(); // in flight
  h.buf.capture(entry("b", 1)); // arms timer T
  h.timers.fireAll(); // T fires while in flight: flush() coalesces, handle must clear
  h.sender.calls[0].reject(new Error("no route"));
  assertEq(await p, false);
  assertEq(h.timers.pending.size, 1, "a retry timer was armed despite T having fired");
  h.timers.fireAll();
  assertEq(h.sender.calls.length, 2, "retry sent");
  assertEq(h.sender.calls[1].batch.map((e) => e.summary), ["a 1", "b 1"]);
});

await test("a second flush during a flight returns the same in-flight promise", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  const p1 = h.buf.flush();
  const p2 = h.buf.flush();
  assert(p1 === p2, "same promise");
  assertEq(h.sender.calls.length, 1, "one send");
  h.sender.calls[0].resolve();
  await p1;
});

await test("success removes exactly the sent entries even when the buffer was reshaped mid-flight", async () => {
  const h = harness(); // maxBacklog 5
  h.buf.capture(entry("a", 1));
  h.buf.capture(entry("a", 2));
  const p = h.buf.flush(); // batch = [a1, a2]
  // A replay lands mid-flight in FRONT and the trim reshapes the buffer, so
  // the sent batch is no longer "the first two".
  h.buf.replay([entry("r", 1), entry("r", 2), entry("r", 3), entry("r", 4), entry("r", 5)]);
  assertEq(h.buf.entries().map((e) => e.summary), ["r 3", "r 4", "r 5", "a 1", "a 2"]);
  h.sender.calls[0].resolve();
  await p;
  // Exactly the sent entries are gone; nothing unsent went missing.
  assertEq(h.buf.entries().map((e) => e.summary), ["r 3", "r 4", "r 5"]);
});

await test("after a success, a full buffer is sent again without waiting for a trigger", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  const p = h.buf.flush();
  h.buf.capture(entry("b", 1));
  h.buf.capture(entry("b", 2));
  h.buf.capture(entry("b", 3)); // size trigger while in flight → coalesced
  assertEq(h.sender.calls.length, 1);
  h.sender.calls[0].resolve();
  await p;
  await tick();
  assertEq(h.sender.calls.length, 2, "follow-up send");
  assertEq(h.sender.calls[1].batch.map((e) => e.summary), ["b 1", "b 2", "b 3"]);
});

console.log("\n── replay ──\n");

await test("replay goes in front, trims the OLDEST off, spools and sends", async () => {
  const h = harness(); // maxBacklog 5
  h.buf.capture(entry("live", 1));
  assertEq(h.buf.replay([1, 2, 3, 4, 5, 6].map((n) => entry("old", n))), true);
  assertEq(h.buf.size, 5, "trimmed to cap");
  assertEq(
    h.buf.entries().map((e) => e.summary),
    ["old 3", "old 4", "old 5", "old 6", "live 1"],
    "old 1 and old 2 dropped, the live entry kept"
  );
  assertEq(h.sender.calls.length, 1, "send started");
  assertEq(h.lastSpool().length, 5);
});

await test("replay reports a spool failure so the caller keeps its source files", async () => {
  const h = harness({ spool: { persist: () => { throw new Error("read-only fs"); } } });
  assertEq(h.buf.replay([entry("old", 1)]), false);
  assertEq(h.buf.size, 1, "entries are still held in memory");
});

await test("replay of nothing does nothing and counts as persisted", async () => {
  const h = harness();
  assertEq(h.buf.replay([]), true);
  assertEq(h.sender.calls.length, 0);
  assertEq(h.spoolWrites.length, 0);
});

await test("replay during a flight leaves a timer for what remains after success", async () => {
  const h = harness(); // flushSize 3
  h.buf.capture(entry("a", 1));
  const p = h.buf.flush();
  h.timers.pending.clear(); // pretend no timer survived into the flight
  h.buf.replay([entry("old", 1)]); // coalesces onto the in-flight promise
  h.sender.calls[0].resolve();
  await p;
  await tick();
  assertEq(h.buf.size, 1, "the replayed entry is still waiting");
  assertEq(h.sender.calls.length, 1, "under flushSize: no immediate send");
  assertEq(h.timers.pending.size, 1, "but a timer will send it");
  h.timers.fireAll();
  assertEq(h.sender.calls.length, 2);
});

console.log("\n── drain ──\n");

await test("drain waits for the in-flight send and then sends what arrived meanwhile", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  h.buf.flush();
  h.buf.capture(entry("b", 1));
  const d = h.buf.drain();
  let done = false;
  d.then(() => (done = true));
  await tick();
  assert(!done, "drain must not resolve while a send is in flight");
  h.sender.calls[0].resolve();
  await tick();
  await tick();
  assertEq(h.sender.calls.length, 2, "tail sent");
  h.sender.calls[1].resolve();
  assertEq(await d, true);
  assertEq(h.buf.size, 0);
});

await test("drain stops at the first failure and reports it", async () => {
  const h = harness();
  h.buf.capture(entry("a", 1));
  const d = h.buf.drain();
  await tick();
  h.sender.calls[0].reject(new Error("down"));
  assertEq(await d, false);
  assertEq(h.buf.size, 1, "entry kept for the retry");
});

await test("drain on an empty buffer resolves true without sending", async () => {
  const h = harness();
  assertEq(await h.buf.drain(), true);
  assertEq(h.sender.calls.length, 0);
});

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed > 0 ? 1 : 0);
