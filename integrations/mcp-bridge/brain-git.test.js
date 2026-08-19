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
import { autoCommitBrainFile, sessionEndCommit } from "./brain-git.js";

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

// ── sessionEndCommit ─────────────────────────────────────────────

const seOpts = {
  brainDir: clone,
  agentId: "test-agent",
  dateStr: "2026-08-19",
};

console.log("\n── sessionEndCommit ──\n");

test("clean tree → 'no changes to commit'", () => {
  const lines = sessionEndCommit(seOpts);
  assert(lines.length === 1 && lines[0].includes("no changes to commit"), lines.join(" | "));
});

test("commits ONLY own files; other agents' dirty work reported, not swept", () => {
  writeFileSync(join(clone, "test-agent-s99.md"), "my session archive\n");
  writeFileSync(join(clone, "other-agent.md"), "someone else's half-written lane\n");
  writeFileSync(join(clone, "active.md"), "shared board, mid-edit\n");
  const lines = sessionEndCommit(seOpts);
  const committed = git(["show", "--name-only", "--format=", "HEAD"], clone);
  assert(committed === "test-agent-s99.md", `committed: ${committed}`);
  assert(lines[0].includes("OK (test-agent-s99.md)"), `line0: ${lines[0]}`);
  assert(
    lines[1] && lines[1].includes("other-agent.md") && lines[1].includes("active.md"),
    `line1: ${lines[1]}`
  );
  const remoteLog = git(["log", "-1", "--format=%s", "master"], bare);
  assert(remoteLog === "brain: test-agent session end — 2026-08-19", `remote: ${remoteLog}`);
  rmSync(join(clone, "other-agent.md"));
  rmSync(join(clone, "active.md"));
});

test("prefix cannot cross agents (agent 'cc' does not match cody-*)", () => {
  writeFileSync(join(clone, "cody-session.md"), "cody's work\n");
  const before = git(["rev-parse", "HEAD"], clone);
  const lines = sessionEndCommit({ ...seOpts, agentId: "cc" });
  assert(git(["rev-parse", "HEAD"], clone) === before, "HEAD moved");
  assert(lines[0].includes("no changes of yours"), `line0: ${lines[0]}`);
  assert(lines[1].includes("cody-session.md"), `line1: ${lines[1]}`);
  rmSync(join(clone, "cody-session.md"));
});

test("brainDir as SUBDIR of the repo → own file found and committed via :/ pathspec", () => {
  const sub = join(clone, "brain");
  writeFileSync(join(sub, "test-agent.md"), "lane edited outside write_brain_file\n");
  writeFileSync(join(clone, "root-junk.md"), "untracked clutter at root\n");
  const lines = sessionEndCommit({ ...seOpts, brainDir: sub });
  const committed = git(["show", "--name-only", "--format=", "HEAD"], clone);
  assert(committed === "brain/test-agent.md", `committed: ${committed}`);
  assert(lines[0].includes("brain/test-agent.md"), `line0: ${lines[0]}`);
  assert(lines[1].includes("root-junk.md"), `line1: ${lines[1]}`);
  rmSync(join(clone, "root-junk.md"));
});

test("push failure → committed locally, loud FAILED status, leftovers still reported", () => {
  git(["remote", "set-url", "origin", join(root, "nonexistent.git")], clone);
  writeFileSync(join(clone, "test-agent-s100.md"), "archive\n");
  writeFileSync(join(clone, "other-agent.md"), "foreign\n");
  const lines = sessionEndCommit(seOpts);
  assert(lines[0].includes("push FAILED") && lines[0].includes("test-agent-s100.md"), `line0: ${lines[0]}`);
  assert(lines[1].includes("other-agent.md"), `line1: ${lines[1]}`);
  git(["remote", "set-url", "origin", bare], clone);
  git(["push"], clone);
  rmSync(join(clone, "other-agent.md"));
});

test("staged rename → BOTH sides land, old name deleted from HEAD", () => {
  writeFileSync(join(clone, "test-agent-s101.md"), "to be renamed\n");
  git(["add", "test-agent-s101.md"], clone);
  git(["commit", "-m", "seed rename source"], clone);
  git(["push"], clone);
  git(["mv", "test-agent-s101.md", "test-agent-s102.md"], clone);
  const lines = sessionEndCommit(seOpts);
  assert(lines[0].startsWith("Brain commit + push: OK"), `line0: ${lines[0]}`);
  const tree = git(["ls-tree", "-r", "--name-only", "HEAD"], clone);
  assert(!tree.includes("test-agent-s101.md"), "old name still in HEAD — rename deletion dropped");
  assert(tree.includes("test-agent-s102.md"), "new name missing from HEAD");
  assert(git(["status", "--porcelain"], clone) === "", "tree not clean after");
});

test("non-ASCII filename → committed cleanly, batch not poisoned", () => {
  writeFileSync(join(clone, "test-agent-café.md"), "accented\n");
  writeFileSync(join(clone, "test-agent-s103.md"), "plain\n");
  const lines = sessionEndCommit(seOpts);
  assert(lines[0].startsWith("Brain commit + push: OK"), `line0: ${lines[0]}`);
  assert(git(["status", "--porcelain"], clone) === "", "left dirty");
});

test("untracked DIRECTORY of own files → expanded and committed, not left as 'dir/'", () => {
  mkdirSync(join(clone, "test-agent-arch"));
  writeFileSync(join(clone, "test-agent-arch", "test-agent-old1.md"), "archived\n");
  const lines = sessionEndCommit(seOpts);
  assert(lines[0].includes("test-agent-arch/test-agent-old1.md"), `line0: ${lines[0]}`);
  assert(git(["status", "--porcelain"], clone) === "", "left dirty");
});

test("own-prefixed file OUTSIDE brainDir → reported with <repo>/ marker, never committed", () => {
  const sub = join(clone, "brain");
  writeFileSync(join(sub, "test-agent.md"), "lane v2\n");
  writeFileSync(join(clone, "test-agent-tool.sh"), "#!/bin/sh\n");
  const lines = sessionEndCommit({ ...seOpts, brainDir: sub });
  const committed = git(["show", "--name-only", "--format=", "HEAD"], clone);
  assert(committed === "brain/test-agent.md", `committed: ${committed}`);
  assert(lines[1].includes("<repo>/test-agent-tool.sh"), `line1: ${lines[1]}`);
  rmSync(join(clone, "test-agent-tool.sh"));
});

test("non-repo dir → skipped, never throws", () => {
  const plain = join(root, "plain-dir-se");
  mkdirSync(plain);
  const lines = sessionEndCommit({ ...seOpts, brainDir: plain });
  assert(lines[0].includes("not a git repo"), lines.join(" | "));
});

rmSync(root, { recursive: true, force: true });

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
