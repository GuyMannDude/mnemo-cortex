// One render for every bridge tool that writes a memory (2.42.0, item 18a).
//
// The server answers a write with HTTP 200 and a `status`. Only ONE status
// per route means "stored": `archived` from /writeback, `saved` from
// /trajectory/save. Everything else is a write that did NOT land: `paused`
// (capture gate discarded it), `superseded` (item 17 W1: the id was demoted,
// the re-send is not stored), `held` (near-duplicate), or a status this
// bridge has never seen. Each tool used to render its own reply with
// `memory_id || "ok"`, so a new status read as a save by default
// (2026-11-18: a paused mnemo_save said "Saved ... memory_id: ok").
//
// So the rule lives here, once: the stored status is the only path that says
// Saved; any other status says NOT SAVED / NOT STORED and prints the raw
// status. A status added to the server later fails closed, not open.

export const STORED_STATUS = Object.freeze({
  writeback: "archived",
  trajectory: "saved",
});

// What a capture pause discards on /writeback (server l.1927): kept in one
// line so every NOT SAVED reply names the same rule.
export const PAUSE_GATE_RULE =
  "while paused, saves with source=tool, category=session_log, or a routine-shaped summary or key_facts are discarded";

function rawStatus(data) {
  if (!data || typeof data !== "object") return "(no reply body)";
  if (data.status === undefined || data.status === null || data.status === "") return "(none)";
  return String(data.status);
}

// -> { stored, status, id, line, isError }
//   stored  true only on the route's stored status
//   line    one-line verdict: "Saved …", "NOT SAVED …", "NOT STORED …", "HELD …"
//   isError true for every write that did not land, except `held` (a review
//           step the caller resolves with force/supersedes, not a failure)
export function writebackVerdict(data, { route = "writeback", agent = "" } = {}) {
  const status = rawStatus(data);
  const message = data && typeof data.message === "string" && data.message ? data.message : "";
  const id = (data && (data.memory_id || data.trajectory_id)) || "";
  const say = message ? ` Server: ${message}` : "";

  if (status === STORED_STATUS[route]) {
    return { stored: true, status, id, isError: false,
      line: `Saved (status=${status}, id=${id || "(none returned)"})` };
  }
  if (status === "held") {
    return { stored: false, status, id: "", isError: false,
      line: `HELD as a near-duplicate — NOT SAVED (status=held).` };
  }
  if (status === "paused") {
    return { stored: false, status, id: "", isError: true,
      line: `NOT SAVED: capture is paused for ${agent || "this agent"} (status=paused)` +
        (route === "writeback"
          ? `; ${PAUSE_GATE_RULE}. Save again after the pause lifts, or now with source=user and a real summary.`
          : ". Save again after the pause lifts.") + say };
  }
  if (status === "superseded") {
    const by = data.superseded_by ? ` by ${data.superseded_by}` : "";
    return { stored: false, status, id, isError: true,
      line: `NOT STORED: ${id || "this memory"} was superseded${by} (status=superseded); ` +
        `this re-send was not stored. If it still needs remembering, save it as new content.` };
  }
  return { stored: false, status, id: "", isError: true,
    line: `NOT SAVED: Mnemo answered status=${status}, which this bridge does not know as stored.${say}` };
}

// The `Mnemo save:` line in session_checkpoint / session_end replies.
export function mnemoSaveLine(data, { agent = "", sid = "", heldHint = "" } = {}) {
  const v = writebackVerdict(data, { route: "writeback", agent });
  if (v.stored) {
    return { ...v, text: `Mnemo save: OK (memory_id=${v.id || "(none returned)"}${sid ? `, session ${sid}` : ""})` };
  }
  if (v.status === "held") {
    const ids = ((data && data.near_duplicates) || []).map((m) => m.id).join(", ");
    return { ...v, text: `Mnemo save: HELD as near-duplicate of ${ids || "existing memories"} — NOT saved. ${heldHint}`.trim() };
  }
  return { ...v, text: `Mnemo save: ${v.line}` };
}

// One stderr line when an auto-capture flush did not land. The buffer still
// drops the entries (a 200 is delivered): for the pausing seat that IS the
// pause; for anyone else it must at least be visible. null = nothing to say.
export function flushNotice(data, count, agent = "") {
  const v = writebackVerdict(data, { route: "writeback", agent });
  if (v.stored) return null;
  if (v.status === "paused") {
    return `[auto-capture] ${count} entries discarded by capture pause (agent ${agent || "default"}, status=paused)`;
  }
  return `[auto-capture] ${count} entries NOT stored (agent ${agent || "default"}, status=${v.status})`;
}
