// seat-clock.js — what the bridge tells the server about WHO wrote a capture
// and WHEN it happened (item 18c'), on its own so it can be tested.
//
// Since 18b' a capture pause is a window on event time, per (tenant, seat).
// A write that names no seat is covered by every window on its tenant, so a
// pause from one seat silenced every seat sharing the tenant. The bridge now
// sends its seat, its own clock (client_now) and, where a write carries past
// events (an auto-capture flush), their event_span. ISO-8601 only: epoch
// milliseconds read as seconds would be a far-future time no window covers.

import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const SEAT_RE = /^[A-Za-z0-9_.-]{1,64}$/;

// The seat: MNEMO_SEAT, else the session prefix (tenant, plus the MNEMO_LANE
// tag: cc3-igor.md on tenant cc -> "cc-cc3"; no lane -> the tenant itself).
// An unusable value sends NO seat — every window on the tenant then covers
// this bridge: over-withheld, never leaked.
export function bridgeSeat(envSeat, prefix) {
  const raw = (envSeat ?? "").trim();
  const seat = raw || prefix;
  if (SEAT_RE.test(seat)) return { seat, warning: null };
  return {
    seat: null,
    warning: `[mnemo-mcp] seat ${JSON.stringify(seat)} is not [A-Za-z0-9_.-]{1,64}: ` +
      `sending NO seat, so every pause on this tenant covers this bridge`,
  };
}

export function toMs(iso) {
  if (typeof iso !== "string" || !iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

// [first, last] event time of a batch, ISO; null when any entry's time cannot
// be read (no span: the server judges the post time, the old rule).
export function eventSpan(entries) {
  const ms = entries.map((e) => toMs(e.ts));
  if (ms.length === 0 || ms.some((t) => t === null)) return null;
  return [new Date(Math.min(...ms)).toISOString(), new Date(Math.max(...ms)).toISOString()];
}

// The seat a capture batch may claim: this bridge's, only when EVERY entry
// was captured under it. A replayed orphan spool can hold another seat's
// entries (spools are claimed per tenant, and CC / CC3 share one), or a
// pre-18c' bridge's (no seat): sent under our seat, a window on THEIR seat
// would not cover them. Those batches go with no seat, so every window on
// the tenant applies (over-withheld, never leaked).
export function batchSeat(entries, seat) {
  return seat && entries.length > 0 && entries.every((e) => e.seat === seat) ? seat : null;
}

// Body fields for a labelled write. A bare body (the 422 re-send) has none.
export function labelled(body, seat, now = new Date()) {
  const out = { ...body, client_now: now.toISOString() };
  if (seat) out.seat = seat;
  return out;
}

export function bare(body) {
  const { seat, client_now, event_span, ...rest } = body;
  return rest;
}

// The session's start for the 422 windows read: the EARLIER of this process's
// start and the oldest event in the payload (a replayed orphan spool carries a
// dead bridge's entries, older than this process). null = cannot be told.
export function sessionStartMs(startedAtMs, entries) {
  const ms = entries.map((e) => toMs(e.ts));
  if (ms.some((t) => t === null)) return null;
  return Math.min(startedAtMs, ...ms);
}

// The 422 rule (CC ruling #7033). The server refused the write's event_span.
// Re-sending it bare is safe only if NO window on the tenant exists since the
// session's start: a bare write is judged on post time, so an in-window event
// posted late would land. Otherwise (or when the read fails) the raw save
// goes to this client's own disk. Either way the caller is told loudly.
//   readWindows(sinceSeconds) -> { windows: [...] }   (GET /capture/windows)
//   resend(bareBody)          -> server reply
//   writeSpool(rawBody)       -> path written
//   A failed re-send or spool write returns action "failed" with its own
//   loud line: the caller says it and keeps the batch for a (visible) retry.
export async function resolve422({ body, detail, sinceMs, readWindows, resend, writeSpool }) {
  const why = `the server refused event_span ${JSON.stringify(body.event_span ?? null)} (422${detail ? `: ${String(detail).slice(0, 160)}` : ""})`;
  let windows = null;
  let readError = null;
  if (sinceMs === null) {
    readError = "the session's start cannot be told";
  } else {
    try {
      const r = await readWindows(sinceMs / 1000);
      if (!r || !Array.isArray(r.windows)) throw new Error("reply has no windows list");
      windows = r.windows;
    } catch (err) {
      readError = err.message;
    }
  }
  if (windows && windows.length === 0) {
    let reply;
    try {
      reply = await resend(bare(body));
    } catch (err) {
      return {
        action: "failed",
        error: err,
        line: `🛑 [mnemo] 422 field event_span: ${why}. No pause window since the session started, but the BARE RE-SEND FAILED (${String(err.message).slice(0, 200)}). Kept for retry. This is a client bug: report it.`,
      };
    }
    return {
      action: "bare",
      reply,
      line: `⚠️ [mnemo] 422 field event_span: ${why}. No pause window on this tenant since the session started, so it was RE-SENT BARE (no seat, no clock). This is a client bug: report it.`,
    };
  }
  const cause = windows
    ? `${windows.length} pause window(s) on this tenant since the session started (${windows.map((w) => w.id).join(", ")})`
    : `the windows read failed (${readError})`;
  let path;
  try {
    path = writeSpool(body);
  } catch (err) {
    return {
      action: "failed",
      error: err,
      line: `🛑 [mnemo] 422 field event_span: ${why}. NOT re-sent: ${cause}. The SPOOL WRITE FAILED (${String(err.message).slice(0, 200)}). Kept for retry. This is a client bug: report it.`,
    };
  }
  return {
    action: "spool",
    path,
    line: `🛑 [mnemo] 422 field event_span: ${why}. NOT re-sent: ${cause}. The raw save is spooled at ${path}. This is a client bug: report it.`,
  };
}

// One auto-capture flush: label it (seat per batchSeat, ISO client_now and
// event_span), post it, and apply the 422 rule. Returns { data } when Mnemo
// answered, or { line } when the 422 rule handled it. Throws (after
// onLine) when the 422 rule could not, so the buffer keeps the batch.
export async function flushBatch({ entries, body, seat, startedAtMs, post, readWindows, resend, writeSpool, onLine }) {
  const span = eventSpan(entries);
  const out = labelled(span ? { ...body, event_span: span } : body, batchSeat(entries, seat));
  try {
    return { data: await post(out) };
  } catch (err) {
    if (err.status !== 422) throw err;
    const r = await resolve422({
      body: out, detail: err.detail, sinceMs: sessionStartMs(startedAtMs, entries),
      readWindows, resend, writeSpool,
    });
    onLine(r.line);
    if (r.action === "failed") throw r.error;
    return { line: r.line };
  }
}

export function spoolWriter(dir, tag) {
  return (body) => {
    mkdirSync(dir, { recursive: true });
    const path = join(dir, `${tag}-${process.pid}-${Date.now()}.json`);
    writeFileSync(path, JSON.stringify(body), { mode: 0o600 });
    return path;
  };
}

// Seam check (doctrine-both-halves-green): the jsonl sync on this machine
// writes its folder -> seat map into its state file. If that map is non-empty
// and none of its seats is this bridge's, a pause here does not cover this
// session's synced lines by seat. Absent or empty map = seatless sync = safe.
export function seamLine(seat, statePath, read = (p) => readFileSync(p, "utf8")) {
  let map;
  try {
    map = JSON.parse(read(statePath)).seat_map;
  } catch {
    return null;
  }
  if (!map || typeof map !== "object" || Object.keys(map).length === 0) return null;
  const seats = Object.values(map);
  if (seat && seats.includes(seat)) return null;
  return `⚠️ seat check: the jsonl sync on this machine maps folders to seats ${JSON.stringify(seats)}, ` +
    `none of them this bridge's (${seat ?? "no seat"}). This session's synced lines are seatless ` +
    `(covered) or mis-seated (NOT covered by this pause): which one cannot be told from here. ` +
    `Fix MNEMO_CC_SEATS or MNEMO_SEAT so they match.`;
}

// The pause reply's scope line for a scoped (not fleet-wide) pause: what the
// server DID. An 18b' server echoes the window and the seat it paused; an
// older one ignores `seat` and pauses the whole tenant.
export function seatScope(data, agent, seat) {
  if (data.window === undefined) {
    return `scope: tenant ${agent}, EVERY seat — this Cortex is older than 18b' and cannot scope a pause by seat; every session and sync writing as ${agent} pauses too`;
  }
  if (data.seat) {
    return `scope: seat ${data.seat} of tenant ${agent}. Other seats of ${agent} that send their seat keep capturing; writers that send no seat are paused too`;
  }
  return `scope: tenant ${agent}, EVERY seat (${seat ? "the server returned no seat" : "this bridge has no usable seat"}) — every session and sync writing as ${agent} pauses too`;
}

// Lines that must reach the agent, not only stderr (an MCP child's stderr
// reaches nobody on most seats): queued, then appended to the next tool reply.
export function createNotices() {
  const pending = [];
  return {
    // A line equal to the last one still pending is dropped: a retry that
    // fails the same way every idle cycle must not pile up hundreds.
    push(line) {
      if (line && pending[pending.length - 1] !== line) pending.push(line);
    },
    get size() {
      return pending.length;
    },
    wrap(handler) {
      return async (...args) => {
        const result = await handler(...args);
        if (pending.length === 0 || !result || !Array.isArray(result.content)) return result;
        const text = pending.splice(0).join("\n");
        return { ...result, content: [...result.content, { type: "text", text }] };
      };
    },
  };
}
