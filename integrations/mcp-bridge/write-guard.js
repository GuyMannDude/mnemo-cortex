// write-guard.js — compare-and-write for write_brain_file.
//
// Why this exists: write_brain_file was last-writer-wins. Two sessions
// read the same brain file, each added its facts, and the second full-file
// rewrite silently dropped the first one's (guy-genealogy.md, 2026-09-22,
// snag-brain-file-clobber-concurrent-sessions). A "re-read before write"
// rule in the file is jazz; the muscle belongs here, on the action.
//
// The bridge is one process per session, so it can remember what THIS
// session has seen: every boot load, read_brain_file, and its own writes
// record a content hash. A write to an existing file is refused when the
// file was never seen this session, or when what is on disk no longer
// hashes to what was seen. Same play as HTTP If-Match. New files pass.
//
// Pure module: no fs, no git. The caller reads the disk and syncs git.

import { createHash } from "node:crypto";

const seen = new Map();

export function contentHash(content) {
  return createHash("sha256").update(content, "utf-8").digest("hex");
}

/** Remember the content this session last saw for `filename`. */
export function recordSeen(filename, content) {
  seen.set(filename, contentHash(content));
}

/** True when `content` is exactly what this session last saw for `filename`. */
export function isCurrent(filename, content) {
  return seen.get(filename) === contentHash(content);
}

/** Test/boot hook: forget everything this session has seen. */
export function resetSeen() {
  seen.clear();
}

/**
 * Decide whether a full-file write may proceed.
 *
 * @param {object} opts
 * @param {string} opts.filename   sanitized flat brain filename
 * @param {string|null} opts.onDisk current file content, or null if absent
 * @param {string} [opts.lastCommit] one-line `git log -1` for the file (context only)
 * @returns {string|null} refusal text, or null when the write may proceed
 */
export function staleWriteRefusal({ filename, onDisk, lastCommit }) {
  if (onDisk === null || onDisk === undefined) return null; // new file
  const was = seen.get(filename);
  const context = lastCommit ? `\nLast commit to it: ${lastCommit}` : "";
  if (!was) {
    return (
      `Refused: ${filename} exists (${onDisk.length} bytes) but this session never read it, ` +
      `so a full-file write would overwrite whatever is there. ` +
      `read_brain_file("${filename}") first, merge your changes into the current text, then write.` +
      context
    );
  }
  if (contentHash(onDisk) !== was) {
    return (
      `Refused: ${filename} changed since this session last read it (now ${onDisk.length} bytes). ` +
      `Another session wrote it. read_brain_file("${filename}") again, merge your changes into the ` +
      `current text, then write — do not resend the old version.` +
      context
    );
  }
  return null;
}
