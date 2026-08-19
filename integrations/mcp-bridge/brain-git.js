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

/**
 * session_end's brain commit. Stages ONLY the ending agent's own files —
 * basename `<agent>.<ext>` or `<agent>-*` (lane, session archives,
 * archive index) — never the shared tree. The brain repo is shared by five
 * agents and a dirty tree is the NORMAL mid-session state; the old
 * `git add -A` here swept every other agent's in-progress edits into a
 * commit under the ending agent's name (snag-session-end-git-add-all).
 * Anything left dirty is REPORTED, not swallowed, and the commit line
 * names exactly what it landed.
 *
 * @param {object} opts
 * @param {string} opts.brainDir  BRAIN_DIR (may be a subdir of the repo)
 * @param {string} opts.agentId   for ownership matching + commit message
 * @param {string} opts.dateStr   local date, e.g. "2026-08-19"
 * @returns {string[]} human-readable status lines — never throws
 */
export function sessionEndCommit({ brainDir, agentId, dateStr }) {
  let insideWorkTree = false;
  try {
    insideWorkTree =
      git(["rev-parse", "--is-inside-work-tree"], brainDir) === "true";
  } catch {
    // git missing or not a repo — fall through to the skip status.
  }
  if (!insideWorkTree)
    return ["Brain commit skipped (brain dir is not a git repo)"];

  let porcelain;
  try {
    // NOT the git() helper: its trim() would eat the leading status column
    // of the first record. `-z` + quotePath=false gives NUL-terminated
    // records with RAW paths — no C-style quoting of non-ASCII names to
    // mis-decode, and a rename's origin arrives as its own NUL field
    // instead of an ambiguous " -> " inside one line. --untracked-files=all
    // expands an untracked DIRECTORY into real file paths ("dir/" has no
    // basename and would otherwise be unclassifiable).
    porcelain = execFileSync(
      "git",
      ["-c", "core.quotePath=false", "status", "--porcelain", "-z", "--untracked-files=all"],
      { cwd: brainDir, encoding: "utf-8", stdio: ["ignore", "pipe", "pipe"] }
    );
  } catch (err) {
    return [`Brain commit FAILED reading git status (${firstLine(err)})`];
  }
  const records = porcelain.split("\0").filter((f) => f.length > 0);
  if (!records.length) return ["Brain commit: no changes to commit"];

  // Porcelain paths are relative to the REPO ROOT while brainDir may be a
  // subdir ("brain/"): staging uses `:/` top-of-tree pathspec magic, and
  // ownership is bounded to files UNDER brainDir via the prefix.
  let prefix = "";
  try {
    prefix = git(["rev-parse", "--show-prefix"], brainDir);
  } catch {
    // best effort — empty prefix means brainDir is treated as the root.
  }

  // needsAdd: the worktree column (Y) differs from the index — untracked,
  // modified, or deleted in the worktree. A path whose change is FULLY
  // staged (Y = " ", e.g. a `git mv`) must NOT be passed to `git add`: a
  // staged rename's origin exists in neither worktree nor index, so add
  // errors "pathspec did not match" — the pathspec COMMIT below already
  // picks up staged state, deletions included.
  const entries = [];
  for (let i = 0; i < records.length; i++) {
    const xy = records[i].slice(0, 2);
    entries.push({ path: records[i].slice(3), needsAdd: xy[1] !== " " });
    // Rename/copy: the ORIGIN path follows as its own field. Both sides
    // must be classified, or a staged rename's deletion is silently
    // dropped and the pushed brain keeps the file under BOTH names.
    if (xy[0] === "R" || xy[0] === "C") {
      i++;
      if (records[i]) entries.push({ path: records[i], needsAdd: false });
    }
  }

  const isMine = (e) => {
    if (!e.path.startsWith(prefix)) return false; // outside brainDir ≠ lane work
    const base = e.path.split("/").pop();
    return base.startsWith(`${agentId}.`) || base.startsWith(`${agentId}-`);
  };
  const mineEntries = entries.filter(isMine);
  const mine = mineEntries.map((e) => e.path);
  const others = entries.filter((e) => !isMine(e)).map((e) => e.path);

  const lines = [];
  if (mine.length) {
    const spec = mine.map((p) => `:/${p}`);
    const toAdd = mineEntries.filter((e) => e.needsAdd).map((e) => `:/${e.path}`);
    try {
      if (toAdd.length) git(["add", "-A", "--", ...toAdd], brainDir);
      git(
        ["commit", "-m", `brain: ${agentId} session end — ${dateStr}`, "--", ...spec],
        brainDir
      );
      try {
        git(["push"], brainDir, { timeout: 15000 });
        lines.push(`Brain commit + push: OK (${mine.join(", ")})`);
      } catch (err) {
        lines.push(
          `Brain commit: OK (${mine.join(", ")}); push FAILED (${firstLine(err)}) — pull/rebase and push manually`
        );
      }
    } catch (err) {
      lines.push(
        `Brain commit FAILED (${firstLine(err)}) — still on disk, uncommitted: ${mine.join(", ")}`
      );
    }
  } else {
    lines.push("Brain commit: no changes of yours to commit");
  }
  if (others.length) {
    // Report brain files by the name write_brain_file knows them by;
    // anything outside brainDir is marked so nobody tries to re-save it
    // through a tool that cannot reach it (its filenames are flat).
    const shown = others.map((p) =>
      p.startsWith(prefix) && prefix ? p.slice(prefix.length) : prefix ? `<repo>/${p}` : p
    );
    lines.push(`⚠️ Left uncommitted (not yours to sweep): ${shown.join(", ")}`);
    const saveable = shown.filter((p) => !p.includes("/"));
    if (saveable.length) {
      lines.push(
        `   If any of these are YOUR OWN edits, re-save via write_brain_file to commit: ${saveable.join(", ")}`
      );
    }
  }
  return lines;
}
