// transcript-watch.js — did a session transcript actually LAND on this host?
//
// Opie (Claude Desktop Cowork) is told to export every session's transcript
// to the host's import folder at every checkpoint. He forgets; Guy had to
// remind him by hand, and the sessions 10-09 → 10-17 were lost from the VM
// before anyone noticed (bus #4864/#4866, Guy 2026-10-19 "let's figure how
// to trigger autosave on Opie's sessions").
//
// The bridge runs ON the host where device_commit_files drops the file, so
// it can verify the export instead of trusting an "I exported". This module
// counts tool calls since the newest transcript file landed and produces a
// nudge once the count passes a threshold. A file landing resets it. Files
// that pre-date the bridge process never count: the watch starts at zero.
//
// Pure logic + injected fs so it is testable without a real Downloads folder.

import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

// A transcript file: <uuid>.jsonl in the import dir, or the Cowork download
// name cowork-transcript-*.jsonl. Backups (.bak-*) never count as a landing.
export function isTranscriptFile(name) {
  if (!name.endsWith(".jsonl")) return false;
  if (name.includes(".bak")) return false;
  return /^[0-9a-f-]{36}\.jsonl$/i.test(name) || name.startsWith("cowork-transcript-");
}

// Every transcript file across dirs: [{ file, mtimeMs }]. A missing or
// unreadable dir is skipped, not an error — the watch must never break a
// tool call.
export function listTranscripts(dirs, fs = { readdirSync, statSync }) {
  const out = [];
  for (const dir of dirs) {
    let names;
    try { names = fs.readdirSync(dir); } catch { continue; }
    for (const name of names) {
      if (!isTranscriptFile(name)) continue;
      let st;
      try { st = fs.statSync(join(dir, name)); } catch { continue; }
      out.push({ file: join(dir, name), mtimeMs: st.mtimeMs });
    }
  }
  return out;
}

// Newest transcript file across dirs: { file, mtimeMs } or null.
export function newestTranscript(dirs, fs = { readdirSync, statSync }) {
  let best = null;
  for (const t of listTranscripts(dirs, fs)) {
    if (!best || t.mtimeMs > best.mtimeMs) best = t;
  }
  return best;
}

export function createTranscriptWatch({
  dirs,
  threshold = 25,
  scanIntervalMs = 10_000,
  fs = { readdirSync, statSync },
  now = () => Date.now(),
  shipInstructions = "",
} = {}) {
  if (!dirs || dirs.length === 0) throw new Error("transcript watch needs at least one dir");
  // A landing is a file that was not there at the last reset (a new name —
  // a copy that keeps its original mtime, scp -p, an unzip, still counts),
  // or a known name whose mtime moved forward (a re-export of the same
  // session). Anything present at reset is a previous session's export.
  let known = new Map();        // file -> mtimeMs as of the last reset/landing
  let landed = null;            // { file, mtimeMs } newest landing THIS session
  let callsSinceLanded = 0;
  let lastScan = 0;
  // Opie's seat can refuse the export outright (a safety classifier blocks
  // the copy as PII handling, bus #4866/#4917). Once the agent reports that,
  // nudging "export" again only produces the same refusal. In denied mode
  // the count becomes calls-since-checkpoint and the nudge asks for a
  // session_checkpoint instead: the chat's substance into Mnemo by hand.
  let denied = false;

  // Claude Desktop keeps ONE bridge process across many chats; agent_startup
  // calls this so a chat never inherits the previous chat's landing, count
  // or denial (reviewer finding, 2.36.0).
  function reset() {
    known = new Map(listTranscripts(dirs, fs).map((t) => [t.file, t.mtimeMs]));
    landed = null;
    callsSinceLanded = 0;
    lastScan = 0;
    denied = false;
  }
  reset();

  function scan(force = false) {
    const t = now();
    if (!force && t - lastScan < scanIntervalMs) return;
    lastScan = t;
    let newest = null;
    for (const f of listTranscripts(dirs, fs)) {
      const prev = known.get(f.file);
      if (prev !== undefined && f.mtimeMs <= prev) continue;   // unchanged
      if (!newest || f.mtimeMs > newest.mtimeMs) newest = f;
    }
    if (newest) {
      landed = newest;
      known.set(newest.file, newest.mtimeMs);
      callsSinceLanded = 0;
    }
  }

  function trackCall() {
    callsSinceLanded++;
    scan();
  }

  // Appended to tool results once the count passes the threshold. Keeps
  // firing on every call until a file lands — the same shape as the memory
  // nudge, and for the same reason: a single reminder is scrolled past.
  function markDenied() {
    denied = true;
    callsSinceLanded = 0;
  }

  // A checkpoint resets the count ONLY in denied mode. Otherwise the file is
  // what counts: a checkpoint that did not ship is still an unshipped session.
  function markCheckpoint() {
    if (denied) callsSinceLanded = 0;
  }

  function nudge() {
    // About to nag: look at the folder NOW, past the rate limit. A nudge
    // that fires after the file landed teaches the agent to ignore it.
    scan(callsSinceLanded >= threshold);
    if (callsSinceLanded < threshold) return null;
    if (denied) {
      return `\n\n---\n📤 **Checkpoint nudge:** ${callsSinceLanded} tool calls since your last checkpoint, and the transcript export is denied from this seat. Call \`session_checkpoint\` now with the chat's substance (decisions, Guy's words, what changed) — that is this session's only record.`;
    }
    const since = landed ? `since the last transcript landed (${landed.file})` : "and NO transcript has landed this session";
    return `\n\n---\n📤 **Transcript nudge:** ${callsSinceLanded} tool calls ${since}. Ship it now${shipInstructions ? `: ${shipInstructions}` : "."} The bridge resets this nudge when the file lands in ${dirs.join(" or ")}.`;
  }

  // One line for session_checkpoint / session_end results.
  function status() {
    scan(true);
    if (denied) return `Transcript: export DENIED from this seat (classifier); ${callsSinceLanded} tool calls since the last checkpoint — checkpoints are the record.`;
    if (landed) {
      const ageMin = Math.max(0, Math.round((now() - landed.mtimeMs) / 60_000));
      return `Transcript: landed ${landed.file} (${ageMin} min ago, ${callsSinceLanded} tool calls since).`;
    }
    return `Transcript: NONE landed this session (${callsSinceLanded} tool calls). Ship it${shipInstructions ? `: ${shipInstructions}` : "."}`;
  }

  return { trackCall, nudge, status, scan, reset, markDenied, markCheckpoint, get denied() { return denied; }, get callsSinceLanded() { return callsSinceLanded; }, get landed() { return landed; } };
}
