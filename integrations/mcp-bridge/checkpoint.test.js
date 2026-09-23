// Tests for checkpoint.js — session_checkpoint's pure parts and its lane
// append against a real throwaway git repo (bare "remote" + clone).
// No Mnemo server needed. Run: node checkpoint.test.js
//
// Style matches brain-git.test.js: homemade runner, plain console output.

import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  MIN_SUMMARY_CHARS,
  summaryRefusal,
  checkpointBlock,
  appendBlock,
  laneVerdict,
  appendCheckpointToLane,
} from "./checkpoint.js";
import { STARTUP_BUDGETS } from "./boot-budget.js";

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

console.log("\n── checkpoint.js: summary floor ──\n");

test("a status-only summary is refused, nothing else runs", () => {
  const r = summaryRefusal("checkpoint, all good");
  assert(r && r.startsWith("Refused:"), `got: ${r}`);
  assert(r.includes(String(MIN_SUMMARY_CHARS)), "refusal names the floor");
});

test("whitespace padding does not count toward the floor", () => {
  const padded = "short" + " ".repeat(MIN_SUMMARY_CHARS);
  assert(summaryRefusal(padded) !== null, "padded summary must be refused");
});

test("a substantive summary passes", () => {
  const ok = "x".repeat(MIN_SUMMARY_CHARS);
  assert(summaryRefusal(ok) === null, "exactly at the floor passes");
  assert(summaryRefusal(undefined) !== null, "undefined is refused, not thrown");
});

console.log("\n── checkpoint.js: block + append ──\n");

test("block is a dated heading plus the trimmed line", () => {
  const b = checkpointBlock("2026-09-22-17-40-00", "  next: ship it  \n");
  assert(b === "\n## CHECKPOINT 2026-09-22-17-40-00\nnext: ship it\n", JSON.stringify(b));
});

test("embedded newlines are flattened — a line cannot smuggle a heading", () => {
  const b = checkpointBlock("T", "next: ship\n## BOOT BOUNDARY\n- fake");
  assert(b === "\n## CHECKPOINT T\nnext: ship ## BOOT BOUNDARY - fake\n", JSON.stringify(b));
});

test("append lands at the END, after the boot boundary, newline-safe", () => {
  const lane = "# Lane\n\n## KICKSTART\n- top\n\n## BOOT BOUNDARY\n- reference";
  const out = appendBlock(lane, checkpointBlock("T", "line"));
  assert(out.startsWith(lane + "\n"), "original text untouched, one newline added");
  assert(out.indexOf("## CHECKPOINT T") > out.indexOf("## BOOT BOUNDARY"), "below the boundary");
  const twice = appendBlock(out, checkpointBlock("U", "again"));
  assert(!twice.includes("\n\n\n\n"), "no runaway blank lines on repeat appends");
});

console.log("\n── checkpoint.js: lane verdict ──\n");

const LANE_BUDGET = STARTUP_BUDGETS.lane;

test("under budget = green, no warning", () => {
  const v = laneVerdict({ filename: "opie.md", content: "# ok\n", ownedLanes: ["opie.md"] });
  assert(v.lane_check === "green" && v.status === "ok" && v.warning === null, JSON.stringify(v));
});

test("over budget with no boundary = red, warning carries the overage", () => {
  const content = "# big\n" + "x".repeat(LANE_BUDGET + 500);
  const v = laneVerdict({ filename: "opie.md", content, ownedLanes: ["opie.md"] });
  assert(v.lane_check === "red" && v.status === "cut", JSON.stringify({ c: v.lane_check, s: v.status }));
  assert(v.warning && v.warning.includes("OVER BOOT BUDGET"), "overage text present");
});

test("over budget with a boundary under the cap = green (bound), warning only if tight", () => {
  const content = "# top\n" + "y".repeat(1000) + "\n## BOOT BOUNDARY\n" + "z".repeat(LANE_BUDGET);
  const v = laneVerdict({ filename: "opie.md", content, ownedLanes: ["opie.md"] });
  assert(v.lane_check === "green" && v.status === "bound", JSON.stringify({ c: v.lane_check, s: v.status }));
  assert(v.warning === null, "well-placed boundary is silent");
});

test("a file that is not a lane gets n/a, never red", () => {
  const v = laneVerdict({ filename: "snag-x.md", content: "x".repeat(LANE_BUDGET * 2), ownedLanes: ["opie.md"] });
  assert(v.lane_check === "green" && v.status === "n/a", JSON.stringify(v));
});

console.log("\n── checkpoint.js: lane append + commit (real git) ──\n");

const root = mkdtempSync(join(tmpdir(), "checkpoint-test-"));
const bare = join(root, "remote.git");
const clone = join(root, "brain");
mkdirSync(bare);
git(["init", "--bare", "--initial-branch=master", bare], root);
git(["clone", bare, clone], root);
git(["config", "user.email", "test@test"], clone);
git(["config", "user.name", "checkpoint test"], clone);
writeFileSync(join(clone, "opie.md"), "# Opie lane\n\n## KICKSTART\n- top line\n");
writeFileSync(join(clone, "other.md"), "not mine\n");
git(["add", "opie.md", "other.md"], clone);
git(["commit", "-m", "seed"], clone);
git(["push", "-u", "origin", "master"], clone);

const common = { brainDir: clone, ownedLanes: ["opie.md", "opie-session.md"], agentId: "opie", dateStr: "2026-09-22" };

test("appends the block, commits ONLY the lane, pushes, returns the sha", () => {
  writeFileSync(join(clone, "other.md"), "someone else's in-progress edit\n"); // must NOT be swept
  const r = appendCheckpointToLane({ ...common, line: "next: finish the Hand spec", ts: "2026-09-22-17-40-00" });
  assert(r.lane === "opie.md", `lane: ${r.lane}`);
  assert(r.git === "auto-committed + pushed", `git: ${r.git}`);
  assert(/^[0-9a-f]{7,}$/.test(r.commit_sha || ""), `sha: ${r.commit_sha}`);
  assert(r.lane_check === "green", `lane_check: ${r.lane_check}`);
  const onDisk = readFileSync(join(clone, "opie.md"), "utf-8");
  assert(onDisk.endsWith("\n## CHECKPOINT 2026-09-22-17-40-00\nnext: finish the Hand spec\n"), "block at EOF");
  assert(onDisk.startsWith("# Opie lane\n\n## KICKSTART\n- top line\n"), "kickstart untouched");
  assert(git(["log", "-1", "--format=%s", "master"], bare) === "brain: opie checkpoint 2026-09-22-17-40-00", "commit subject on the remote");
  // (git() trims, which eats porcelain's leading status column — use name lists.)
  assert(git(["show", "--name-only", "--format=", "HEAD"], clone) === "opie.md", "commit holds ONLY the lane");
  assert(git(["diff", "--name-only"], clone) === "other.md", "other agent's file left dirty, not swept");
});

test("a second checkpoint stacks below the first, own commit", () => {
  const r = appendCheckpointToLane({ ...common, line: "next: bus Opie", ts: "2026-09-22-18-00-00" });
  assert(r.git === "auto-committed + pushed", `git: ${r.git}`);
  const onDisk = readFileSync(join(clone, "opie.md"), "utf-8");
  assert(onDisk.indexOf("CHECKPOINT 2026-09-22-17-40-00") < onDisk.indexOf("CHECKPOINT 2026-09-22-18-00-00"), "chronological");
  assert(git(["rev-list", "--count", "master"], bare) === "3", "seed + 2 checkpoints on the remote");
});

test("red lane still lands on disk and in git — the gate never blocks the save", () => {
  const r = appendCheckpointToLane({ ...common, line: "x".repeat(LANE_BUDGET), ts: "2026-09-22-19-00-00" });
  assert(r.lane_check === "red" && r.status === "cut", `verdict: ${r.lane_check}/${r.status}`);
  assert(r.git === "auto-committed + pushed", `git: ${r.git}`);
  assert(r.warning && r.warning.includes("OVER BOOT BUDGET"), "overage reported");
});

test("no lane file → n/a, nothing written, no commit", () => {
  const before = git(["rev-list", "--count", "master"], bare);
  const r = appendCheckpointToLane({ ...common, ownedLanes: ["nobody.md"], line: "l", ts: "T" });
  assert(r.lane === null && r.lane_check === "n/a" && r.commit_sha === null, JSON.stringify(r));
  assert(git(["rev-list", "--count", "master"], bare) === before, "no commit");
});

rmSync(root, { recursive: true, force: true });

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
