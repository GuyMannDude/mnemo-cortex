// brain-git.js — auto-commit + push for write_brain_file.
//
// Why this exists: a lane file written via write_brain_file but never
// committed is invisible to every other agent until someone lands it by
// hand (an agent's rewrite once sat on disk for two days while its own
// text complained the repo was behind). session_end commits, but long
// sessions don't always reach session_end. So the write itself commits —
// a hard check instead of a polite reminder.
//
// Fail-soft by design: the file is already safely on disk before this
// runs, so no git failure (not a repo, no remote, offline, dirty merge
// state) is ever allowed to turn a successful write into an error. The
// caller appends the returned status string to the tool response so the
// agent knows whether follow-up is needed.

import { execFileSync } from "node:child_process";

function git(args, cwd, extra = {}) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "pipe"],
    ...extra,
  }).trim();
}

function firstLine(err) {
  const msg = (err && (err.stderr || err.message)) || String(err);
  return String(msg).trim().split("\n")[0];
}

/**
 * Stage, commit, and push a single brain file. Commits ONLY the named
 * path (pathspec commit), so unrelated staged changes are never swept up.
 *
 * @param {object} opts
 * @param {string} opts.brainDir  BRAIN_DIR (may be a subdir of the repo)
 * @param {string} opts.filename  sanitized filename relative to brainDir
 * @param {string} opts.agentId   for the commit message
 * @param {string} opts.dateStr   local date, e.g. "2026-07-17"
 * @returns {string} human-readable status — never throws
 */
export function autoCommitBrainFile({ brainDir, filename, agentId, dateStr }) {
  let insideWorkTree = false;
  try {
    insideWorkTree =
      git(["rev-parse", "--is-inside-work-tree"], brainDir) === "true";
  } catch {
    // git missing or not a repo — fall through to the skip status.
  }
  if (!insideWorkTree) return "auto-commit skipped (brain dir is not a git repo)";

  try {
    git(["add", "--", filename], brainDir);
    try {
      // Exit 0 = nothing staged for this path (content identical).
      git(["diff", "--cached", "--quiet", "--", filename], brainDir);
      return "auto-commit skipped (no changes vs last commit)";
    } catch {
      // Non-zero exit = staged changes exist — proceed to commit.
    }
    git(
      [
        "commit",
        "-m",
        `brain: ${agentId} updated ${filename} via write_brain_file — ${dateStr}`,
        "--",
        filename,
      ],
      brainDir
    );
  } catch (err) {
    return `auto-commit FAILED (${firstLine(err)}) — file IS written to disk; commit + push manually or via session_end`;
  }

  try {
    // Timeout so a network stall degrades to the fail-soft status below
    // instead of freezing the tool response (this runs on EVERY write).
    git(["push"], brainDir, { timeout: 15000 });
    return "auto-committed + pushed";
  } catch (err) {
    return `committed locally; push FAILED (${firstLine(err)}) — pull/rebase and push manually, or session_end will report it`;
  }
}
