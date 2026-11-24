// Tests for seat-clock.js — seat, clock and event span on bridge writes, the
// 422 rule (CC #7033), the seat seam check and the notice queue (item 18c').
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  bridgeSeat, eventSpan, labelled, bare, sessionStartMs, resolve422,
  seamLine, seatScope, createNotices, batchSeat, flushBatch,
} from "./seat-clock.js";

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;

test("seat: MNEMO_SEAT wins, else the session prefix; a bad one sends none", () => {
  assert.deepEqual(bridgeSeat("linus-a", "cc-cc3"), { seat: "linus-a", warning: null });
  assert.deepEqual(bridgeSeat(undefined, "cc-cc3"), { seat: "cc-cc3", warning: null });
  assert.deepEqual(bridgeSeat("  ", "cc"), { seat: "cc", warning: null });
  const bad = bridgeSeat("has space", "cc");
  assert.equal(bad.seat, null);
  assert.match(bad.warning, /sending NO seat/);
});

test("labelled body: ISO client_now + seat; bare strips seat, clock and span", () => {
  const b = labelled({ summary: "x", event_span: ["a", "b"] }, "cc-cc3", new Date(1795476000000));
  assert.match(b.client_now, ISO);
  assert.equal(b.seat, "cc-cc3");
  assert.equal(typeof b.client_now, "string", "never epoch ms on the wire");
  assert.deepEqual(bare(b), { summary: "x" });
  assert.equal(labelled({}, null).seat, undefined, "no seat key when none");
});

test("event span: ISO [first, last] of the entries; unreadable time = no span", () => {
  const span = eventSpan([
    { ts: "2026-11-23T23:40:00.000Z" }, { ts: "2026-11-23T23:30:00.000Z" }, { ts: "2026-11-23T23:35:00.000Z" },
  ]);
  assert.deepEqual(span, ["2026-11-23T23:30:00.000Z", "2026-11-23T23:40:00.000Z"]);
  for (const v of span) assert.match(v, ISO);
  assert.equal(eventSpan([{ ts: "2026-11-23T23:40:00Z" }, { ts: "nope" }]), null);
  assert.equal(eventSpan([]), null);
});

test("session start: the earlier of process start and the oldest entry", () => {
  const started = Date.parse("2026-11-23T23:00:00Z");
  assert.equal(sessionStartMs(started, [{ ts: "2026-11-23T23:10:00Z" }]), started);
  // a replayed orphan spool carries a dead bridge's older entries
  assert.equal(sessionStartMs(started, [{ ts: "2026-11-23T20:00:00Z" }]), Date.parse("2026-11-23T20:00:00Z"));
  assert.equal(sessionStartMs(started, [{ ts: "garbage" }]), null);
});

function harness({ windows, readFails = false }) {
  const h = { reads: [], resent: [], spooled: [] };
  h.args = {
    body: { summary: "s", seat: "linus-a", client_now: "2026-11-23T23:40:00Z", event_span: [1795476000000, 1795476000001] },
    detail: "event_span values must be ISO-8601 or epoch SECONDS",
    sinceMs: Date.parse("2026-11-23T23:00:00Z"),
    readWindows: async (since) => {
      h.reads.push(since);
      if (readFails) throw new Error("Mnemo Cortex returned 401: no");
      return { windows };
    },
    resend: async (b) => { h.resent.push(b); return { status: "archived", memory_id: "m1" }; },
    writeSpool: (b) => { h.spooled.push(b); return "/scratch/422-spool/x.json"; },
  };
  return h;
}

test("422 + no window: re-sent BARE with the loud line", async () => {
  const h = harness({ windows: [] });
  const out = await resolve422(h.args);
  assert.equal(out.action, "bare");
  assert.deepEqual(h.resent, [{ summary: "s" }], "no seat, no clock, no span");
  assert.equal(h.spooled.length, 0);
  assert.equal(h.reads[0], Date.parse("2026-11-23T23:00:00Z") / 1000, "since = session start, epoch SECONDS");
  assert.match(out.line, /422 field event_span/);
  assert.match(out.line, /RE-SENT BARE/);
});

test("422 + a window: nothing re-sent, the spool holds the raw save", async () => {
  const h = harness({ windows: [{ id: "w-1" }] });
  const out = await resolve422(h.args);
  assert.equal(out.action, "spool");
  assert.equal(h.resent.length, 0);
  assert.deepEqual(h.spooled, [h.args.body], "raw save, as sent");
  assert.match(out.line, /event_span/);
  assert.match(out.line, /\/scratch\/422-spool\/x\.json/);
  assert.match(out.line, /w-1/);
});

test("422 + the windows read fails: same as a window", async () => {
  const h = harness({ windows: [], readFails: true });
  const out = await resolve422(h.args);
  assert.equal(out.action, "spool");
  assert.equal(h.resent.length, 0);
  assert.equal(h.spooled.length, 1);
  assert.match(out.line, /windows read failed/);
});

test("422 + unknowable session start: spool, no read", async () => {
  const h = harness({ windows: [] });
  const out = await resolve422({ ...h.args, sinceMs: null });
  assert.equal(out.action, "spool");
  assert.equal(h.reads.length, 0);
  assert.equal(h.resent.length, 0);
});

test("422 + a reply with no windows list: treated as a failed read", async () => {
  const h = harness({ windows: [] });
  const out = await resolve422({ ...h.args, readWindows: async () => ({}) });
  assert.equal(out.action, "spool");
});

test("seam check: silent when the sync map is absent, empty, or holds this seat", () => {
  const read = (m) => () => JSON.stringify({ seat_map: m });
  assert.equal(seamLine("cc-cc3", "/x", () => { throw new Error("ENOENT"); }), null);
  assert.equal(seamLine("cc-cc3", "/x", read({})), null);
  assert.equal(seamLine("cc-cc3", "/x", () => "{}"), null);
  assert.equal(seamLine("cc-cc3", "/x", read({ "-home-guy-CC3": "cc-cc3", "-home-guy": "cc" })), null);
  const line = seamLine("cc-cc9", "/x", read({ "-home-guy": "cc" }));
  assert.match(line, /seat check/);
  assert.match(line, /cc-cc9/);
  assert.match(seamLine(null, "/x", read({ "-home-guy": "cc" })), /no seat/);
});

test("pause scope line says what the server did", () => {
  assert.match(seatScope({ window: "w-1", seat: "linus-a" }, "t", "linus-a"), /seat linus-a of tenant t/);
  assert.match(seatScope({ fleet_wide: false }, "t", "linus-a"), /older than 18b'/);
  assert.match(seatScope({ window: "w-1", seat: null }, "t", null), /EVERY seat/);
});

test("notices ride on the next tool reply, once", async () => {
  const n = createNotices();
  const tool = n.wrap(async () => ({ content: [{ type: "text", text: "reply" }] }));
  assert.deepEqual((await tool()).content.length, 1, "nothing pending: untouched");
  n.push("🛑 [mnemo] 422 field event_span");
  n.push(null);
  const r = await tool();
  assert.equal(r.content.length, 2);
  assert.match(r.content[1].text, /422 field event_span/);
  assert.equal((await tool()).content.length, 1, "delivered once");
});

test("422 + the bare re-send fails: 'failed' with its own loud line", async () => {
  const h = harness({ windows: [] });
  const out = await resolve422({ ...h.args, resend: async () => { throw new Error("HTTP 400"); } });
  assert.equal(out.action, "failed");
  assert.match(out.line, /BARE RE-SEND FAILED \(HTTP 400\)/);
  assert.match(out.line, /event_span/);
});

test("422 + the spool write fails: 'failed' with its own loud line", async () => {
  const h = harness({ windows: [{ id: "w-1" }] });
  const out = await resolve422({ ...h.args, writeSpool: () => { throw new Error("ENOSPC"); } });
  assert.equal(out.action, "failed");
  assert.match(out.line, /SPOOL WRITE FAILED \(ENOSPC\)/);
});

test("batch seat: ours only when every entry was captured under it", () => {
  const a = { seat: "cc-cc3" };
  assert.equal(batchSeat([a, a], "cc-cc3"), "cc-cc3");
  assert.equal(batchSeat([a, { seat: "cc" }], "cc-cc3"), null, "another seat's replayed spool");
  assert.equal(batchSeat([a, {}], "cc-cc3"), null, "a pre-18c' spool entry");
  assert.equal(batchSeat([], "cc-cc3"), null);
  assert.equal(batchSeat([a], null), null);
});

function flushHarness({ postStatus = 200, windows = [], resendThrows = false } = {}) {
  const h = { posts: [], resent: [], spooled: [], lines: [] };
  h.args = {
    entries: [
      { tool: "t", summary: "s", ts: "2026-11-23T23:30:00.000Z", seat: "linus-a" },
      { tool: "t", summary: "s", ts: "2026-11-23T23:31:00.000Z", seat: "linus-a" },
    ],
    body: { summary: "[AUTO-CAPTURE] 2", source: "tool" },
    seat: "linus-a",
    startedAtMs: Date.parse("2026-11-23T23:00:00Z"),
    post: async (b) => {
      h.posts.push(b);
      if (postStatus === 422) {
        const e = new Error("Mnemo Cortex returned 422");
        e.status = 422;
        e.detail = "event_span";
        throw e;
      }
      if (postStatus !== 200) throw Object.assign(new Error(`returned ${postStatus}`), { status: postStatus });
      return { status: "archived", memory_id: "m1" };
    },
    readWindows: async () => ({ windows }),
    resend: async (b) => {
      if (resendThrows) throw new Error("down");
      h.resent.push(b);
      return { status: "archived" };
    },
    writeSpool: (b) => { h.spooled.push(b); return "/s/x.json"; },
    onLine: (l) => h.lines.push(l),
  };
  return h;
}

test("flush: posts seat, ISO client_now and the entries' ISO event_span", async () => {
  const h = flushHarness();
  const r = await flushBatch(h.args);
  assert.equal(r.data.status, "archived");
  const b = h.posts[0];
  assert.equal(b.seat, "linus-a");
  assert.deepEqual(b.event_span, ["2026-11-23T23:30:00.000Z", "2026-11-23T23:31:00.000Z"]);
  assert.match(b.client_now, ISO);
  assert.equal(h.lines.length, 0, "quiet");
});

test("flush: a replayed batch with another seat's entries goes with NO seat", async () => {
  const h = flushHarness();
  h.args.entries.push({ tool: "t", summary: "s", ts: "2026-11-23T23:20:00.000Z", seat: "cc" });
  await flushBatch(h.args);
  assert.equal(h.posts[0].seat, undefined);
  assert.equal(h.posts[0].event_span[0], "2026-11-23T23:20:00.000Z");
});

test("flush: 422 + no window lands bare, says so, since = earliest of start and entries", async () => {
  const h = flushHarness({ postStatus: 422 });
  let since;
  h.args.readWindows = async (s) => { since = s; return { windows: [] }; };
  const r = await flushBatch(h.args);
  assert.equal(r.data, undefined);
  assert.equal(h.resent.length, 1);
  assert.equal(h.resent[0].seat, undefined);
  assert.equal(h.resent[0].event_span, undefined);
  assert.match(h.lines[0], /RE-SENT BARE/);
  assert.equal(since, Date.parse("2026-11-23T23:00:00Z") / 1000);
});

test("flush: 422 + a window spools the labelled save, nothing re-sent", async () => {
  const h = flushHarness({ postStatus: 422, windows: [{ id: "w-9" }] });
  await flushBatch(h.args);
  assert.equal(h.resent.length, 0);
  assert.equal(h.spooled[0].seat, "linus-a");
  assert.match(h.lines[0], /spooled at \/s\/x\.json/);
});

test("flush: 422 rule fails -> loud line AND a throw (the buffer keeps the batch)", async () => {
  const h = flushHarness({ postStatus: 422, resendThrows: true });
  await assert.rejects(flushBatch(h.args), /down/);
  assert.match(h.lines[0], /BARE RE-SEND FAILED/);
});

test("flush: other HTTP errors throw untouched (normal retry)", async () => {
  const h = flushHarness({ postStatus: 503 });
  await assert.rejects(flushBatch(h.args), /503/);
  assert.equal(h.lines.length, 0);
});

test("notices: a repeat of the last pending line is dropped", () => {
  const n = createNotices();
  n.push("same");
  n.push("same");
  n.push("other");
  n.push("same");
  assert.equal(n.size, 3);
});
