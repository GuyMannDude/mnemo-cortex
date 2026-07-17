// Tests for write_brain_file's auto-commit. No Mnemo server needed —
// exercises brain-git.js against real throwaway git repos (a local bare
// "remote" + a clone standing in for the brain checkout).
// Run: node brain-git.test.js
//
// Style matches boot-budget.test.js: homemade runner, plain console output.

import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { autoCommitBrainFile } from "./brain-git.js";

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  PASS  ${name}`);
    passed++;
  } catch (err) {
    console.log(`  FAIL  ${name}: ${err.message}`);
    failed++;
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function git(args, cwd) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf-8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

// ── Fixture: bare "remote" + clone with one initial commit ──────

const root = mkdtempSync(join(tmpdir(), "brain-git-test-"));
const bare = join(root, "remote.git");
const clone = join(root, "brain");
mkdirSync(bare);
git(["init", "--bare", "--initial-branch=master", bare], root);
git(["clone", bare, clone], root);
git(["config", "user.email", "test@test"], clone);
git(["config", "user.name", "brain-git test"], clone);
writeFileSync(join(clone, "README.md"), "seed\n");
git(["add", "README.md"], clone);
git(["commit", "-m", "seed"], clone);
git(["push", "-u", "origin", "master"], clone);

const opts = (filename) => ({
  brainDir: clone,
  filename,
  agentId: "test-agent",
  dateStr: "2026-07-17",
});

console.log("\n── brain-git.js ──\n");

test("new file → auto-committed + pushed (visible on the remote)", () => {
  writeFileSync(join(clone, "test-agent.md"), "session 1\n");
  const status = autoCommitBrainFile(opts("test-agent.md"));
  assert(status === "auto-committed + pushed", `status: ${status}`);
  const remoteLog = git(["log", "-1", "--format=%s", "master"], bare);
  assert(
    remoteLog ===
      "brain: test-agent updated test-agent.md via write_brain_file — 2026-07-17",
    `remote log: ${remoteLog}`
  );
});

test("identical rewrite → skipped, no empty commit", () => {
  const before = git(["rev-parse", "HEAD"], clone);
  writeFileSync(join(clone, "test-agent.md"), "session 1\n");
  const status = autoCommitBrainFile(opts("test-agent.md"));
  assert(status.includes("no changes"), `status: ${status}`);
  assert(git(["rev-parse", "HEAD"], clone) === before, "HEAD moved");
});

test("only the named file is committed (unrelated staged work untouched)", () => {
  writeFileSync(join(clone, "unrelated.md"), "someone else's staged edit\n");
  git(["add", "unrelated.md"], clone);
  writeFileSync(join(clone, "test-agent.md"), "session 2\n");
  const status = autoCommitBrainFile(opts("test-agent.md"));
  assert(status === "auto-committed + pushed", `status: ${status}`);
  const committed = git(["show", "--name-only", "--format=", "HEAD"], clone);
  assert(committed === "test-agent.md", `committed: ${committed}`);
  const stillStaged = git(["diff", "--cached", "--name-only"], clone);
  assert(stillStaged === "unrelated.md", `staged: ${stillStaged}`);
  git(["reset", "unrelated.md"], clone);
  rmSync(join(clone, "unrelated.md"));
});

test("push failure → committed locally, loud FAILED status", () => {
  git(["remote", "set-url", "origin", join(root, "nonexistent.git")], clone);
  writeFileSync(join(clone, "test-agent.md"), "session 3\n");
  const status = autoCommitBrainFile(opts("test-agent.md"));
  assert(status.startsWith("committed locally; push FAILED"), `status: ${status}`);
  const localLog = git(["log", "-1", "--format=%s"], clone);
  assert(localLog.includes("session end") === false && localLog.includes("test-agent.md"), `local log: ${localLog}`);
  git(["remote", "set-url", "origin", bare], clone);
});

test("brainDir as SUBDIR of the repo (production shape) → pathspec still isolated", () => {
  // Real config: BRAIN_DIR = <repo>/brain with .git at the repo root.
  const sub = join(clone, "brain");
  mkdirSync(sub);
  writeFileSync(join(clone, "root-work.md"), "staged at repo root\n");
  git(["add", "root-work.md"], clone);
  writeFileSync(join(sub, "test-agent.md"), "subdir session\n");
  const status = autoCommitBrainFile({ ...opts("test-agent.md"), brainDir: sub });
  assert(status === "auto-committed + pushed", `status: ${status}`);
  const committed = git(["show", "--name-only", "--format=", "HEAD"], clone);
  assert(committed === "brain/test-agent.md", `committed: ${committed}`);
  const stillStaged = git(["diff", "--cached", "--name-only"], clone);
  assert(stillStaged === "root-work.md", `staged: ${stillStaged}`);
  git(["reset", "root-work.md"], clone);
  rmSync(join(clone, "root-work.md"));
});

test("non-repo brain dir → skipped, never throws", () => {
  const plain = join(root, "plain-dir");
  mkdirSync(plain);
  writeFileSync(join(plain, "x.md"), "x\n");
  const status = autoCommitBrainFile({ ...opts("x.md"), brainDir: plain });
  assert(status.includes("not a git repo"), `status: ${status}`);
});

rmSync(root, { recursive: true, force: true });

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
