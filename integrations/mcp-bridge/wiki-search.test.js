// Regression tests for wiki-search.js (GHSA-h2jf-xmmf-2w8p: shell injection
// through the wiki_search query). No Mnemo server needed — runs grep against
// a throwaway wiki directory.
// Run: node wiki-search.test.js
//
// Style matches brain-git.test.js: homemade runner, plain console output.

import { mkdtempSync, writeFileSync, mkdirSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { wikiGrepFiles, wikiGrepContext } from "./wiki-search.js";

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

// ── Fixture: a tiny wiki ──────────────────────────────────────
const wiki = mkdtempSync(join(tmpdir(), "wiki-search-test-"));
mkdirSync(join(wiki, "projects"));
mkdirSync(join(wiki, "entities"));
const page = join(wiki, "projects", "peter-widget.md");
writeFileSync(page, "# Peter Widget\nThe widget talks to Rocky.\nSecond mention of the widget.\n");
writeFileSync(join(wiki, "entities", "rocky.md"), "# Rocky\nRocky runs the widget.\n");
writeFileSync(join(wiki, "entities", "notes.txt"), "widget widget widget\n");
const canary = join(wiki, "pwned");

console.log("\n── wiki-search.js ──\n");

test("plain word finds the .md pages, not the .txt", () => {
  const files = wikiGrepFiles("widget", wiki, 10);
  assert(files.length === 2, `expected 2 files, got ${files.length}: ${files}`);
  assert(files.every((f) => f.endsWith(".md")), `non-.md hit: ${files}`);
});

test("search is case-insensitive", () => {
  assert(wikiGrepFiles("ROCKY", wiki, 10).length === 2, "ROCKY should match both pages");
});

test("section directory narrows the search", () => {
  const files = wikiGrepFiles("widget", join(wiki, "entities"), 10);
  assert(files.length === 1 && files[0].endsWith("rocky.md"), `got ${files}`);
});

test("limit caps the file list", () => {
  assert(wikiGrepFiles("widget", wiki, 1).length === 1, "limit 1 should give 1 file");
});

test("no match returns an empty list, not an error", () => {
  assert(wikiGrepFiles("no-such-term-xyzzy", wiki, 10).length === 0, "expected []");
});

test("context has numbered lines and is capped", () => {
  const ctx = wikiGrepContext("widget", page);
  assert(/^1:# Peter Widget/m.test(ctx), `missing line 1: ${ctx}`);
  assert(ctx.split("\n").length <= 12, "context over 12 lines");
  assert(wikiGrepContext("widget", page, 2).split("\n").length === 2, "maxLines 2 not honoured");
});

// ── The advisory: shell metacharacters in the query are text ────
const shapes = {
  "dollar-paren": `x$(touch ${canary})`,
  "backticks": "x`touch " + canary + "`",
  "double-quote break": `x"; touch ${canary}; echo "`,
  "single-quote break": `x'; touch ${canary}; echo '`,
  "pipe": `x | touch ${canary}`,
  "and-and": `x && touch ${canary}`,
  "semicolon": `x; touch ${canary}`,
  "newline": `x\ntouch ${canary}`,
};

for (const [name, q] of Object.entries(shapes)) {
  test(`query shape "${name}" runs nothing (file search)`, () => {
    rmSync(canary, { force: true });
    const files = wikiGrepFiles(q, wiki, 10);
    assert(!existsSync(canary), "CANARY FILE CREATED — shell ran the query");
    assert(files.length === 0, `unexpected hits: ${files}`);
  });
  test(`query shape "${name}" runs nothing (context)`, () => {
    rmSync(canary, { force: true });
    wikiGrepContext(q, page);
    assert(!existsSync(canary), "CANARY FILE CREATED — shell ran the query");
  });
}

test("a query starting with a dash is a pattern, not a grep option", () => {
  const files = wikiGrepFiles("-r", wiki, 10); // would be --recursive if parsed as an option
  assert(files.length === 0, `got ${files}`);
  assert(wikiGrepFiles("--include=*.txt", wiki, 10).length === 0, "option-shaped query matched");
});

test("a path-shaped query stays a pattern (no path traversal)", () => {
  assert(wikiGrepFiles("../../etc/passwd", wiki, 10).length === 0, "path-shaped query matched");
});

test("$HOME in a query is not expanded", () => {
  writeFileSync(join(wiki, "entities", "home.md"), "literal $HOME here\n");
  const files = wikiGrepFiles("$HOME", wiki, 10);
  assert(files.length === 1 && files[0].endsWith("home.md"), `expected the literal page, got ${files}`);
});

rmSync(wiki, { recursive: true, force: true });

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
