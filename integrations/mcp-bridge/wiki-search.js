// wiki-search.js — grep for the wiki tools, without a shell.
//
// GHSA-h2jf-xmmf-2w8p (reported by Artur Chlebicki, @Artur12555): wiki_search
// built a shell command string with the query inside double quotes. $(...) and
// backticks still expand inside double quotes, so a search term ran commands
// as the bridge user. grep now receives argv directly; the query is one
// argument after -e, so a leading dash or any shell character is plain text.

import { execFileSync } from "node:child_process";

function grep(args, timeout) {
  try {
    return execFileSync("grep", args, {
      encoding: "utf-8",
      timeout,
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch (err) {
    if (err.status === 1) return ""; // grep exit 1 = no match
    throw err; // exit 2 (bad pattern, unreadable path) or timeout
  }
}

// Paths of *.md files under searchDir matching query (case-insensitive,
// grep basic regex — same semantics as before), at most `limit` of them.
export function wikiGrepFiles(query, searchDir, limit, timeout = 10000) {
  const out = grep(["-ril", "--include=*.md", "-e", query, "--", searchDir], timeout).trim();
  return out ? out.split("\n").slice(0, limit) : [];
}

// Numbered context lines (one line either side) for query in filePath,
// capped at maxLines.
export function wikiGrepContext(query, filePath, maxLines = 12, timeout = 5000) {
  const out = grep(["-in", "-C", "1", "-e", query, "--", filePath], timeout).trim();
  return out.split("\n").slice(0, maxLines).join("\n");
}
