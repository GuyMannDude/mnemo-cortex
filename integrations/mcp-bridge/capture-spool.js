// capture-spool.js — write-through spool for the auto-capture buffer.
//
// Why this exists: the bridge held its auto-capture trail in a process-memory
// array and drained it on SIGTERM. On IGOR's 2026-09-21 reboot Tailscale tore
// down its routes one second before the bridge was stopped, the drain had no
// route, and process.exit killed the retry. Anything in memory was gone.
//
// The spool mirrors the buffer on disk: rewritten on every capture and every
// flush attempt, removed when the buffer is empty. A bridge that dies with
// entries unsent leaves a file behind; the next bridge to start finds it,
// replays it, and removes it. Write-through survives SIGKILL, route loss and
// reboot without adding a cadence or a daemon.
//
// One file per bridge process (agent + pid + start time in the name) because
// Claude Desktop runs two bridges for the same agent at once and they must
// not clobber each other's file.

import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";

export const SPOOL_PREFIX = "capture-spool-";
export const SPOOL_SUFFIX = ".jsonl";

// A claim renames `<spool>.jsonl` to `<spool>.jsonl.claimed-<claimerPid>-<ms>`.
const CLAIM_RE_SRC = "\\.claimed-(\\d+)-(\\d+)";

function safeAgent(agentId) {
  return String(agentId).replace(/[^A-Za-z0-9_-]/g, "_");
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Unique per process: pid alone repeats across reboots, and a reused pid would
// make a dead bridge's spool look like our own.
export function spoolFile(dir, agentId, pid, startedAt = Date.now()) {
  return join(dir, `${SPOOL_PREFIX}${safeAgent(agentId)}-${pid}-${startedAt}${SPOOL_SUFFIX}`);
}

// When did this pid's process start, in epoch ms? Linux only (reads
// /proc/<pid>/stat field 22, ticks since boot, plus btime from /proc/stat;
// CLK_TCK is 100 on every Linux we run). null when it cannot be known.
function linuxProcessStartMs(pid) {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    const rest = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    const startTicks = Number(rest[19]);
    const btime = Number(/^btime (\d+)/m.exec(readFileSync("/proc/stat", "utf8"))[1]);
    if (!Number.isFinite(startTicks) || !Number.isFinite(btime)) return null;
    return (btime + startTicks / 100) * 1000;
  } catch {
    return null;
  }
}

// Is the process that wrote this pid at time `notAfter` still running? A pid
// alone is not an identity: after a reboot the dead bridge's number is handed
// to whatever starts early, and that daemon may live forever. So on Linux the
// pid also has to have started no later than the moment it was recorded; a
// process that started after that is a reuse, not the writer. Where the start
// time cannot be read, a live pid is trusted (never replay under a live
// writer). EPERM from the signal probe means alive, owned by someone else.
export function defaultPidAlive(pid, notAfter) {
  try {
    process.kill(pid, 0);
  } catch (err) {
    if (err.code !== "EPERM") return false;
  }
  const started = linuxProcessStartMs(pid);
  if (started !== null && started > notAfter + 1000) return false;
  return true;
}

// Mirror `entries` to `file`. Empty buffer = no file, so a clean exit leaves
// nothing behind and a leftover file always means unsent entries.
export function persistSpool(file, entries) {
  if (entries.length === 0) {
    if (existsSync(file)) unlinkSync(file);
    return;
  }
  mkdirSync(dirname(file), { recursive: true });
  // Write beside, then rename over: a power cut mid-write leaves the previous
  // spool intact instead of a truncated one. The temp name does not end in
  // SPOOL_SUFFIX, so a scan never sees it.
  const tmp = `${file}.tmp`;
  writeFileSync(tmp, entries.map((e) => JSON.stringify(e)).join("\n") + "\n");
  renameSync(tmp, file);
}

// One JSON object per line. A line that does not parse, or parses to
// something that is not a capture entry, is counted, not thrown — one bad
// line must not sink the good ones around it.
export function parseSpool(text) {
  const entries = [];
  let bad = 0;
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      const e = JSON.parse(t);
      if (e && typeof e.tool === "string" && typeof e.summary === "string") entries.push(e);
      else bad++;
    } catch {
      bad++;
    }
  }
  return { entries, bad };
}

// Spools with no writer behind them, CLAIMED for this caller.
//
// Only this agent's spools: all bridges on a machine share the state dir, and
// replaying another agent's trail would write it under the wrong agent_id.
// A live bridge touches its spool on every capture and every flush attempt
// (success removes it, failure rewrites it), so a spool untouched for longer
// than the flush cycle has no writer — unless its pid is still that bridge,
// which happens when the laptop slept (timers pause, mtimes do not); those
// are left alone. Each candidate is renamed to a claimed name before it is
// read, so two bridges scanning at once cannot both replay one file: the
// rename is atomic and the loser gets ENOENT. The caller removes the claimed
// file once the entries are safe in its own spool. A claimed file that was
// left behind (claimer died, or could not persist, or could not unlink) is a
// candidate again once its claimer is gone, so nothing is stranded for good.
// One unreadable file is skipped, not fatal to the scan.
// Returned oldest-first so a replay keeps the original order.
export function orphanSpools(
  dir,
  { agentId, ownFile, staleMs, now = Date.now(), pidAlive = defaultPidAlive, claimerPid = process.pid }
) {
  if (!existsSync(dir)) return [];
  const own = new RegExp(
    `^${escapeRe(SPOOL_PREFIX + safeAgent(agentId))}-(\\d+)-(\\d+)${escapeRe(SPOOL_SUFFIX)}(?:${CLAIM_RE_SRC})?$`
  );
  const found = [];
  for (const name of readdirSync(dir)) {
    const m = own.exec(name);
    if (!m) continue;
    const file = join(dir, name);
    if (file === ownFile) continue;
    let st;
    try {
      st = statSync(file);
    } catch {
      continue; // removed between readdir and stat — another bridge got it
    }
    if (now - st.mtimeMs < staleMs) continue;
    const [writerPid, writerStart, prevClaimer, claimedAt] = m.slice(1).map(Number);
    if (pidAlive(writerPid, writerStart)) continue;
    if (m[3] !== undefined && pidAlive(prevClaimer, claimedAt)) continue; // still being replayed
    const base = m[3] === undefined ? file : file.slice(0, file.length - (m[0].length - m[0].indexOf(".claimed-")));
    const claimed = `${base}.claimed-${claimerPid}-${now}`;
    try {
      renameSync(file, claimed);
    } catch {
      continue; // another bridge claimed it first
    }
    let text;
    try {
      text = readFileSync(claimed, "utf8");
    } catch {
      continue; // unreadable now; stays claimed by us and is retried once we are gone
    }
    const { entries, bad } = parseSpool(text);
    found.push({ file: claimed, entries, bad, mtimeMs: st.mtimeMs });
  }
  return found.sort((a, b) => a.mtimeMs - b.mtimeMs);
}
