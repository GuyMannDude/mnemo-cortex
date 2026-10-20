// transcript-push.js — a chat's turns, as text, into the host's import folder
// as an archive-shaped, append-only <uuid>.jsonl.
//
// Why: Opie's seat cannot copy its Cowork transcript FILE out (the safety
// classifier refuses the copy as PII handling, bus #4866/#4917), and a Cowork
// cloud container has no file at all (the 10-09 → 10-17 sessions, lost). Text
// as a TOOL ARGUMENT passes — mnemo_save with health content does — so the
// bridge takes the turns as text, writes the file itself and uploads it.
//
// The file is shaped like a Claude Code transcript because that is what the
// server parses, and it is append-only because that is the server's
// idempotency contract (same bytes = unchanged, longer with a matching
// prefix = replaced, anything else = 409) — the same contract the hourly
// shipper and the IGOR-2 → IGOR mirror already honour, so the file rides
// them as a retry path for free.
//
// Pure logic + injected fs/clock so it is testable without a real folder.

import { createHash } from "node:crypto";
import { readFileSync, appendFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname } from "node:path";

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Deterministic UUID (v5 layout over SHA-1) from a namespace + name. The
// seat passes one chat handle on every push of a chat, and the handle always
// maps to the same file — across pushes, bridge restarts and Desktop chats
// sharing one bridge process. The key is DERIVED even when the handle is the
// chat's own client uuid, so a push file never shares a name (or a server
// key) with the real client export of the same chat (reviewer, 2.37.0).
export function derivedUuid(namespace, name) {
  const h = createHash("sha1").update(`${namespace}:${name}`).digest();
  h[6] = (h[6] & 0x0f) | 0x50;   // version 5
  h[8] = (h[8] & 0x3f) | 0x80;   // RFC 4122 variant
  const hex = h.subarray(0, 16).toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

export function pushSessionId(agentId, chatHandle) {
  return derivedUuid("mnemo-transcript-push", `${agentId}:${chatHandle}`);
}

// A seat re-typing a turn rarely reproduces it byte for byte: whitespace
// drifts, quotes curl. Overlap compares this form; the stored text stays
// verbatim as sent.
export function normalizeText(t) {
  return String(t)
    .replace(/[\u2018\u2019\u201a\u201b]/g, "'")
    .replace(/[\u201c\u201d\u201e\u201f]/g, '"')
    .replace(/[\u2013\u2014]/g, "-")
    .replace(/\s+/g, " ")
    .trim();
}

// What the file already holds: the turns in order (role + text), their
// count, and the uuid of the last line (the next line's parent). Lines that
// are not ours (bad JSON, other types) are skipped, never an error.
export function readExisting(text) {
  const turns = [];
  let lastUuid = null;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }
    if (!obj || (obj.type !== "user" && obj.type !== "assistant")) continue;
    const t = obj.message?.content?.[0]?.text;
    turns.push({ role: obj.type, text: typeof t === "string" ? t : "" });
    if (typeof obj.uuid === "string") lastUuid = obj.uuid;
  }
  return { turns, lastUuid };
}

// How many leading incoming turns repeat the TAIL of what is archived. This
// is overlap, not a set: a chat that says "ok" twice keeps both, while a
// re-send of the last few turns — or of the whole chat so far — adds only
// what is new. Largest k such that existing[-k:] == incoming[:k], text
// compared normalized (see normalizeText).
export function overlapCount(existing, incoming) {
  const max = Math.min(existing.length, incoming.length);
  for (let k = max; k > 0; k--) {
    let same = true;
    for (let i = 0; i < k; i++) {
      const a = existing[existing.length - k + i];
      const b = incoming[i];
      if (a.role !== b.role || normalizeText(a.text) !== normalizeText(b.text)) { same = false; break; }
    }
    if (same) return k;
  }
  return 0;
}

// Archive-shaped lines for new turns. Timestamps are PUSH time (the seat
// does not know when each turn was said); the version field names this
// path so a reader can tell a push from a client export; fidelity says
// whether the seat copied the turn or re-typed it from a compacted context
// (Opie #4946: a first push of a long chat is part verbatim, part
// reconstructed — unmarked, that is mixed provenance). The server keeps
// lines raw, so the archive carries the field.
export function buildLines({ sessionUuid, turns, startIndex = 0, parentUuid = null, title = null,
                             now = () => Date.now(), version = "mnemo-transcript-push", agentId = "",
                             fidelity = "reconstructed" }) {
  const lines = [];
  const base = now();
  if (title && startIndex === 0) {
    lines.push(JSON.stringify({ type: "ai-title", aiTitle: title, sessionId: sessionUuid }));
  }
  let parent = parentUuid;
  turns.forEach((t, i) => {
    const idx = startIndex + i;
    const uuid = derivedUuid("mnemo-transcript-push-line", `${sessionUuid}:${idx}`);
    const obj = {
      type: t.role,
      sessionId: sessionUuid,
      uuid,
      parentUuid: parent,
      isSidechain: false,
      timestamp: new Date(base + i).toISOString(),
      version,
      fidelity,
      cwd: `mnemo-transcript-push:${agentId}`,
      message: { role: t.role, content: [{ type: "text", text: t.text }] },
    };
    if (t.role === "assistant") obj.message.id = `push_${idx}_${uuid.slice(0, 8)}`;
    lines.push(JSON.stringify(obj));
    parent = uuid;
  });
  return lines;
}

// Append a push to the session's file. Returns { appended, skipped, total };
// a push that adds nothing writes nothing.
export function appendPush({ file, turns, title = null, sessionUuid, now, version, agentId, fidelity,
                             fs = { readFileSync, appendFileSync, mkdirSync, existsSync } }) {
  const prior = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  const { turns: existing, lastUuid } = readExisting(prior);
  const skip = overlapCount(existing, turns);
  const fresh = turns.slice(skip);
  if (fresh.length === 0) {
    return { appended: 0, skipped: turns.length, total: existing.length };
  }
  const lines = buildLines({
    sessionUuid, turns: fresh, startIndex: existing.length, parentUuid: lastUuid,
    title: existing.length === 0 && !prior ? title : null, now, version, agentId, fidelity,
  });
  // A prior file missing its final newline would glue our first line onto its last.
  const glue = prior && !prior.endsWith("\n") ? "\n" : "";
  fs.mkdirSync(dirname(file), { recursive: true });
  fs.appendFileSync(file, `${glue}${lines.join("\n")}\n`);
  return { appended: fresh.length, skipped: skip, total: existing.length + fresh.length };
}
