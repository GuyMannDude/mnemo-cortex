// checkpoint.js — session_checkpoint: save-and-commit WITHOUT closing the session.
//
// Why this exists: session_end was the only save-and-commit, so a session
// that never ends never commits. Guy runs many Cowork sessions open at once;
// the 2026-09-21 IGOR reboot ate one whole (Opie #3709). A checkpoint is the
// same save-and-commit, mid-session, with the session left open: same
// session_id, no rotation, session_end still runs afterwards.
//
// The failure mode it must not enable is the status-only save ("checkpoint,
// all good") — a memory that holds nothing. Hence the length floor: the tool
// cannot see the chat, so substance has to arrive in the summary.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { autoCommitBrainFile } from "./brain-git.js";
import { assess, budgetWarning } from "./write-budget.js";

export const MIN_SUMMARY_CHARS = 400;

// null when the summary carries enough to be worth a memory; otherwise the
// refusal text. Trimmed length — padding with whitespace does not count.
export function summaryRefusal(summary) {
  const n = (summary || "").trim().length;
  if (n >= MIN_SUMMARY_CHARS) return null;
  return (
    `Refused: checkpoint summary is ${n} chars; the floor is ${MIN_SUMMARY_CHARS}. ` +
    `A checkpoint holds what this session learned and decided — write the substance ` +
    `(what changed, why, what is next), not a status line. Nothing was saved.`
  );
}

// The block appended to the lane. A dated heading so lane-index/lane-check
// readers can tell a checkpoint from the owner's KICKSTART, then the line.
export function checkpointBlock(ts, line) {
  // One line means one line: embedded newlines would let a stray "#" become
  // a heading in the lane (and a fake BOOT BOUNDARY is not impossible).
  const flat = line.replace(/\s*\n+\s*/g, " ").trim();
  return `\n## CHECKPOINT ${ts}\n${flat}\n`;
}

// Append at the END of the file — always below any BOOT BOUNDARY, never
// inside the owner's kickstart block at the top.
export function appendBlock(text, block) {
  const base = text.endsWith("\n") ? text : `${text}\n`;
  return base + block;
}

// green/red in lane-check.py's terms: red = content will be silently cut
// at boot (over budget with no boundary, or a boundary past the cut).
// `tight` and a well-placed `bound` stay green but carry the warning text.
export function laneVerdict({ filename, content, ownedLanes }) {
  const a = assess({ filename, content, ownedLanes });
  const warning = budgetWarning({ filename, content, ownedLanes });
  const red = Boolean(a && (a.status === "cut" || a.status === "lies"));
  return { lane_check: red ? "red" : "green", status: a ? a.status : "n/a", warning };
}

function headSha(brainDir) {
  try {
    return execFileSync("git", ["rev-parse", "--short", "HEAD"], {
      cwd: brainDir,
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return null;
  }
}

/**
 * Append a CHECKPOINT block to the agent's lane, commit + push that one
 * path, and report the lane's boot-budget verdict on the result.
 * Never throws on git failure (brain-git.js is fail-soft); throws only if
 * the lane cannot be read or written, which the caller reports.
 *
 * @returns {{lane: string|null, git: string, commit_sha: string|null,
 *            lane_check: "green"|"red"|"n/a", status: string, warning: string|null}}
 */
export function appendCheckpointToLane({ brainDir, ownedLanes, line, ts, agentId, dateStr }) {
  let lane = null;
  for (const c of ownedLanes) {
    if (existsSync(join(brainDir, c))) { lane = c; break; }
  }
  if (!lane) {
    return {
      lane: null,
      git: `no lane file found (${ownedLanes.join(", ")}) — nothing appended`,
      commit_sha: null,
      lane_check: "n/a",
      status: "n/a",
      warning: null,
    };
  }
  const path = join(brainDir, lane);
  const before = readFileSync(path, "utf-8");
  const after = appendBlock(before, checkpointBlock(ts, line));
  writeFileSync(path, after, "utf-8");
  const git = autoCommitBrainFile({
    brainDir,
    filename: lane,
    agentId,
    dateStr,
    message: `brain: ${agentId} checkpoint ${ts}`,
  });
  const committed = git.startsWith("auto-committed") || git.startsWith("committed locally");
  const verdict = laneVerdict({ filename: lane, content: after, ownedLanes });
  // before/after let write_brain_file's guard judge the append without a re-read.
  return { lane, git, commit_sha: committed ? headSha(brainDir) : null, before, after, ...verdict };
}
