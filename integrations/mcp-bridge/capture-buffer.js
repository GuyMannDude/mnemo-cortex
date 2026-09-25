// capture-buffer.js — the auto-capture buffer's state machine, on its own so
// it can be tested against a stubbed sender.
//
// Rules it keeps:
//   - An entry leaves the buffer only after `send` has accepted the batch it
//     was in. A crash mid-flight loses nothing the spool did not already hold.
//   - Only one send is in flight at a time; callers that ask while one is
//     running get that same promise. `drain()` loops until the buffer is empty
//     or an attempt fails.
//   - Sent entries are removed by identity, not by count, so a trim or a
//     replay that reshapes the buffer during a send cannot make the success
//     path delete entries that were never sent.
//   - The spool (`spool.persist(entries)`) is rewritten after every change to
//     the buffer. A spool failure is logged, never thrown into a tool call;
//     `replay` reports it so the caller can keep its source files.
//   - Whenever the buffer is non-empty and nothing is in flight, an idle timer
//     is armed. A timer that fires into an in-flight send must not leave a
//     dead handle behind, or the retry it stands for never comes.

export function createCaptureBuffer({
  send,
  spool,
  log = () => {},
  flushSize = 8,
  idleMs = 120_000,
  maxBacklog = 200,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  const buffer = [];
  let idleTimer = null;
  let inFlight = null;

  function persist() {
    try {
      spool.persist(buffer);
      return true;
    } catch (err) {
      log(`[auto-capture] spool write failed: ${err.message}`);
      return false;
    }
  }

  // Keep the most recent activity when a long outage piles entries up.
  function trim() {
    if (buffer.length > maxBacklog) buffer.splice(0, buffer.length - maxBacklog);
  }

  // The handle is cleared as the timer fires, so `idleTimer` is only ever a
  // live timer or null.
  function arm() {
    if (idleTimer) clearTimer(idleTimer);
    idleTimer = setTimer(() => {
      idleTimer = null;
      flush();
    }, idleMs);
  }

  function capture(entry) {
    buffer.push(entry);
    persist();
    arm();
    if (buffer.length >= flushSize) flush();
  }

  // Entries recovered from a dead bridge's spool. They are older than anything
  // already here, so they go in FRONT: a trim drops the oldest first and a
  // batch goes out in order. Returns whether the spool now holds them.
  function replay(entries) {
    if (entries.length === 0) return true;
    buffer.unshift(...entries);
    trim();
    const ok = persist();
    arm();
    flush();
    return ok;
  }

  async function sendOnce() {
    const batch = buffer.slice();
    try {
      await send(batch);
    } catch (err) {
      log(`[auto-capture] flush failed (will retry): ${err.message}`);
      trim();
      persist();
      if (!idleTimer) arm();
      return false;
    }
    for (const e of batch) {
      const i = buffer.indexOf(e);
      if (i !== -1) buffer.splice(i, 1);
    }
    persist();
    log(`[auto-capture] flushed ${batch.length} entries`);
    return true;
  }

  // Resolves true when the buffer's contents at call time reached Mnemo.
  function flush() {
    if (inFlight) return inFlight;
    if (buffer.length === 0) return Promise.resolve(true);
    if (idleTimer) {
      clearTimer(idleTimer);
      idleTimer = null;
    }
    inFlight = sendOnce().then((ok) => {
      inFlight = null;
      if (ok && buffer.length >= flushSize) flush();
      // Whatever is left (arrived or replayed mid-flight) must not wait for
      // the next capture to get a timer.
      else if (buffer.length > 0 && !idleTimer) arm();
      return ok;
    });
    return inFlight;
  }

  // Everything, including what arrives while sending. Stops at the first
  // failed attempt (a retry is already scheduled by then).
  async function drain() {
    while (buffer.length > 0) {
      if (!(await flush())) return false;
    }
    return true;
  }

  return {
    capture,
    replay,
    flush,
    drain,
    get size() {
      return buffer.length;
    },
    get inFlight() {
      return inFlight !== null;
    },
    entries: () => buffer.slice(),
  };
}
