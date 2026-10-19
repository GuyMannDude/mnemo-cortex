// Tests for transcript-watch.js — the "did the transcript land?" nudge.
// Run: node transcript-watch.test.js
//
// Style matches lane-guard.test.js: homemade runner, plain console output.
// The fs is faked so no real folders are touched.

import { join } from "node:path";
import { createTranscriptWatch, isTranscriptFile, newestTranscript } from "./transcript-watch.js";

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

// Fake fs: { "/dir": { "name.jsonl": mtimeMs } }
function fakeFs(tree) {
  return {
    readdirSync: (dir) => {
      if (!(dir in tree)) throw new Error("ENOENT");
      return Object.keys(tree[dir]);
    },
    statSync: (p) => {
      for (const dir of Object.keys(tree)) {
        for (const name of Object.keys(tree[dir])) {
          if (p === join(dir, name)) return { mtimeMs: tree[dir][name] };   // join: same separator as the module (Windows too)
        }
      }
      throw new Error("ENOENT");
    },
  };
}

test("transcript file names: uuid.jsonl and cowork-transcript-*.jsonl count, backups and others do not", () => {
  assert(isTranscriptFile("f63341ea-be21-5693-b93b-fd7af1ad5091.jsonl"), "uuid.jsonl");
  assert(isTranscriptFile("cowork-transcript-2026-10-19.jsonl"), "cowork download name");
  assert(!isTranscriptFile("b6014bf8-8685-53f7-9145-ab84566c45b6.jsonl.bak-20260929T124912Z-cc-pre-merge"), "backup");
  assert(!isTranscriptFile("notes.jsonl"), "random jsonl");
  assert(!isTranscriptFile("photo.png"), "not jsonl");
});

test("newestTranscript picks the newest across dirs and skips a missing dir", () => {
  const fs = fakeFs({ "/imp": { "f63341ea-be21-5693-b93b-fd7af1ad5091.jsonl": 100 }, "/dl": { "cowork-transcript-x.jsonl": 250, "junk.jsonl": 999 } });
  const n = newestTranscript(["/imp", "/dl", "/missing"], fs);
  assert(n && n.file === join("/dl", "cowork-transcript-x.jsonl") && n.mtimeMs === 250, `got ${JSON.stringify(n)}`);
  assert(newestTranscript(["/missing"], fs) === null, "no dirs readable → null");
});

test("files that pre-date the process never count as a landing", () => {
  const tree = { "/imp": { "f63341ea-be21-5693-b93b-fd7af1ad5091.jsonl": 100 } };
  let t = 1000;
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 3, scanIntervalMs: 0, fs: fakeFs(tree), now: () => t });
  w.trackCall(); w.trackCall(); w.trackCall();
  assert(w.landed === null, "old file must not count");
  assert(w.nudge() !== null, "nudge fires at threshold");
  assert(w.nudge().includes("NO transcript has landed this session"), "wording for never-landed");
});

test("below threshold there is no nudge; at threshold it fires and keeps firing", () => {
  const tree = { "/imp": {} };
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 3, scanIntervalMs: 0, fs: fakeFs(tree) });
  w.trackCall(); w.trackCall();
  assert(w.nudge() === null, "2 calls < 3");
  w.trackCall();
  assert(w.nudge() !== null, "3 calls fires");
  w.trackCall();
  assert(w.nudge() !== null && w.nudge().includes("4 tool calls"), "keeps firing with the count");
});

test("a file landing after start resets the count and the nudge stops", () => {
  const tree = { "/imp": {} };
  let t = 1000;
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 2, scanIntervalMs: 0, fs: fakeFs(tree), now: () => t });
  w.trackCall(); w.trackCall();
  assert(w.nudge() !== null, "fires before landing");
  tree["/imp"]["72e27466-0000-4000-8000-000000000000.jsonl"] = 5000;  // newer than baseline 0
  w.trackCall();
  assert(w.landed && w.landed.file.endsWith("72e27466-0000-4000-8000-000000000000.jsonl"), "landing seen");
  assert(w.callsSinceLanded === 0, `count reset, got ${w.callsSinceLanded}`);
  assert(w.nudge() === null, "nudge stops");
  t = 1000 + 3 * 60_000;
  assert(w.status().includes("landed") && w.status().includes("3 min ago"), `status: ${w.status()}`);
});

test("a second, newer landing resets again; the same file does not", () => {
  const tree = { "/imp": { "72e27466-0000-4000-8000-000000000000.jsonl": 5000 } };
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 2, scanIntervalMs: 0, fs: fakeFs(tree) });
  w.trackCall(); w.trackCall();
  assert(w.nudge() !== null, "pre-existing file does not count, nudge fires");
  tree["/imp"]["72e27466-0000-4000-8000-000000000000.jsonl"] = 6000;  // overwrite = re-export
  w.trackCall();
  assert(w.callsSinceLanded === 0, "overwrite with newer mtime counts as a landing");
  w.trackCall(); w.trackCall();
  assert(w.callsSinceLanded === 2, "same mtime does not reset again");
});

test("scan is rate-limited between calls but forced by status()", () => {
  const tree = { "/imp": {} };
  let t = 0;
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 1, scanIntervalMs: 10_000, fs: fakeFs(tree), now: () => t });
  w.trackCall();                     // scan at t=0
  tree["/imp"]["72e27466-0000-4000-8000-000000000000.jsonl"] = 1;
  t = 5000;
  w.trackCall();                     // within interval: no scan, still unseen
  assert(w.landed === null, "rate-limited scan did not run");
  assert(w.status().includes("landed"), "status forces a scan");
});

test("a nudge at threshold forces a scan, so it never fires after the file already landed", () => {
  const tree = { "/imp": {} };
  let t = 0;
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 2, scanIntervalMs: 10_000, fs: fakeFs(tree), now: () => t });
  w.trackCall(); w.trackCall();          // scan ran at t=0 only
  tree["/imp"]["72e27466-0000-4000-8000-000000000000.jsonl"] = 1;
  t = 1000;                              // still inside the rate limit
  assert(w.nudge() === null, "nudge saw the landing despite the rate limit");
  assert(w.callsSinceLanded === 0, "count reset");
});

test("status before any landing says NONE and carries the ship instructions", () => {
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 99, fs: fakeFs({ "/imp": {} }), shipInstructions: "cp X then device_commit_files" });
  assert(w.status().startsWith("Transcript: NONE landed this session"), w.status());
  assert(w.status().includes("device_commit_files"), "instructions present");
});

test("no dirs is a construction error, not a silent no-op", () => {
  let threw = false;
  try { createTranscriptWatch({ dirs: [] }); } catch { threw = true; }
  assert(threw, "must throw");
});

test("a NEW file name counts as a landing even with an OLD mtime (copy that kept its timestamp)", () => {
  const tree = { "/imp": { "f63341ea-be21-5693-b93b-fd7af1ad5091.jsonl": 9000 } };
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 1, scanIntervalMs: 0, fs: fakeFs(tree) });
  w.trackCall();
  assert(w.nudge() !== null, "fires before landing");
  tree["/imp"]["72e27466-0000-4000-8000-000000000000.jsonl"] = 100;   // older than everything, but new
  w.trackCall();
  assert(w.landed && w.landed.file.endsWith("72e27466-0000-4000-8000-000000000000.jsonl"), "new name landed");
  assert(w.nudge() === null, "nudge cleared");
});

test("reset() forgets the previous chat: landing, count and denial all clear, files on disk become baseline", () => {
  const tree = { "/imp": {} };
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 2, scanIntervalMs: 0, fs: fakeFs(tree) });
  w.trackCall(); w.trackCall();
  tree["/imp"]["72e27466-0000-4000-8000-000000000000.jsonl"] = 500;
  w.trackCall();
  assert(w.landed !== null, "landed in chat 1");
  w.markDenied();
  w.reset();
  assert(w.landed === null && w.callsSinceLanded === 0 && w.denied === false, "all state cleared");
  assert(w.status().startsWith("Transcript: NONE"), `chat 2 must not report chat 1's file: ${w.status()}`);
  w.trackCall(); w.trackCall();
  assert(w.nudge() !== null && w.nudge().includes("Transcript nudge"), "chat 2 is asked to export again, not checkpoint");
  assert(w.landed === null, "chat 1's file does not count for chat 2");
});

test("denied mode: checkpoint resets the count and the nudge asks for a checkpoint, not an export", () => {
  const w = createTranscriptWatch({ dirs: ["/imp"], threshold: 2, scanIntervalMs: 0, fs: fakeFs({ "/imp": {} }) });
  w.trackCall(); w.trackCall();
  assert(w.nudge().includes("Transcript nudge"), "export wording before denial");
  w.markCheckpoint();
  assert(w.callsSinceLanded === 2, "a checkpoint does NOT reset while export is still possible");
  w.markDenied();
  assert(w.callsSinceLanded === 0 && w.denied, "denial resets and flips the mode");
  w.trackCall(); w.trackCall();
  assert(w.nudge().includes("Checkpoint nudge") && w.nudge().includes("session_checkpoint"), `wording: ${w.nudge()}`);
  assert(w.status().includes("DENIED"), w.status());
  w.markCheckpoint();
  assert(w.callsSinceLanded === 0 && w.nudge() === null, "checkpoint resets in denied mode");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
