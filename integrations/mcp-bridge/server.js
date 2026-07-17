import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createHarnessToolGate } from "./harness-tool-filter.js";
import { z } from "zod";
import { readFile, readdir, writeFile, stat } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { execSync, execFileSync } from "node:child_process";
import { DumpWriter } from "./dump.js";
import { STARTUP_BUDGETS, capSection } from "./boot-budget.js";
import { autoCommitBrainFile } from "./brain-git.js";

// ── Configuration ──────────────────────────────────────────────
// MNEMO_URL: where your Mnemo Cortex API lives
// MNEMO_AGENT_ID: who this agent is in the memory system
// MNEMO_SHARE: cross-agent sharing mode (separate|always|never)
// BRAIN_DIR / WIKI_DIR: optional local knowledge dirs for the
//   read_brain_file / wiki_* tools. Skip if you don't have a
//   sparks-brain checkout — those tools simply error gracefully.
//
// OpenClaw users set these via env vars in their MCP config.

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";
const AGENT_ID = process.env.MNEMO_AGENT_ID || "openclaw";
// MNEMO_AUTH_TOKEN: optional bearer token for the Mnemo Cortex API. Read from
// env first, else from ~/.mnemo-auth-token (mode 0600) so the secret lives in
// one file rather than every agent's MCP config. Sent as X-API-KEY on every
// request. When the server has no auth_token configured the header is ignored,
// so it is safe to set this before the server begins enforcing auth.
const AUTH_TOKEN = (() => {
  if (process.env.MNEMO_AUTH_TOKEN) return process.env.MNEMO_AUTH_TOKEN.trim();
  const home = process.env.HOME || process.env.USERPROFILE || ".";
  const tokenFile = join(home, ".mnemo-auth-token");
  if (existsSync(tokenFile)) {
    try {
      return readFileSync(tokenFile, "utf8").trim();
    } catch {
      /* unreadable token file — fall through to no auth */
    }
  }
  return "";
})();
// BRAIN_DIR defaults to ~/mnemo-plan/brain (matches the public mnemo-plan
// template repo at github.com/GuyMannDude/mnemo-plan). Set BRAIN_DIR
// explicitly in your MCP config to point at any other brain checkout.
const BRAIN_DIR =
  process.env.BRAIN_DIR ||
  join(process.env.HOME || ".", "mnemo-plan/brain");
const WIKI_DIR = process.env.WIKI_DIR || join(process.env.HOME || ".", "wiki");
const DREAM_DIR =
  process.env.DREAM_DIR || join(process.env.HOME || ".", ".agentb/dreams");

// Lane freshness is reported on EVERY boot (Guy, 2026-07-05: "every agent
// every time — I notice when the last session is missing"). Older than this
// many days escalates the always-on freshness line to a warning banner.
const LANE_BEHIND_DAYS = 1;

const SHARE_MODES = ["separate", "always", "never"];
const shareMode = SHARE_MODES.includes(process.env.MNEMO_SHARE)
  ? process.env.MNEMO_SHARE
  : "separate";
let sessionShareActive = shareMode === "always";

const FETCH_TIMEOUT_MS = 10_000;
const MAX_RESPONSE_CHARS = 16_000;

// ── Optional integrations ──────────────────────────────────────
// Brain-lane and wiki tools only register when the directories
// they target actually exist. New users get a clean memory bridge
// (mnemo + passport = 9 tools). Sparks operators with a brain or
// wiki checkout get the rest automatically — same install, more tools.

async function dirExists(path) {
  try {
    const s = await stat(path);
    return s.isDirectory();
  } catch {
    return false;
  }
}

const BRAIN_AVAILABLE = await dirExists(BRAIN_DIR);
const WIKI_AVAILABLE = await dirExists(WIKI_DIR);

if (BRAIN_AVAILABLE) {
  process.stderr.write(`[mnemo-mcp] Brain dir found at ${BRAIN_DIR} — brain/session tools enabled\n`);
}
if (WIKI_AVAILABLE) {
  process.stderr.write(`[mnemo-mcp] Wiki dir found at ${WIKI_DIR} — wiki tools enabled\n`);
}

// ── Mnemo API client ───────────────────────────────────────────
// 10-second timeout on all requests. Errors surface as tool
// errors — the agent sees a clean message, not a stack trace.

async function mnemoRequest(method, path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

  const headers = { "Content-Type": "application/json" };
  if (AUTH_TOKEN) headers["X-API-KEY"] = AUTH_TOKEN;
  const opts = {
    method,
    headers,
    signal: controller.signal,
  };
  if (body) opts.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(`${MNEMO_URL}${path}`, opts);
  } catch (err) {
    clearTimeout(timer);
    if (err.name === "AbortError") {
      process.stderr.write(
        `[mnemo-mcp] Request timed out: ${method} ${path} (${FETCH_TIMEOUT_MS}ms) to ${MNEMO_URL}\n`
      );
      throw new Error(
        "Mnemo Cortex request timed out. The server may be overloaded or unreachable."
      );
    }
    process.stderr.write(
      `[mnemo-mcp] Connection failed: ${method} ${path} to ${MNEMO_URL} — ${err.message}\n`
    );
    throw new Error("Cannot reach Mnemo Cortex. Is it running?");
  }
  clearTimeout(timer);

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    process.stderr.write(
      `[mnemo-mcp] HTTP error: ${method} ${path} → ${res.status}: ${text}\n`
    );
    throw new Error(`Mnemo Cortex returned ${res.status}: ${text}`);
  }

  let data;
  try {
    data = await res.json();
  } catch {
    process.stderr.write(
      `[mnemo-mcp] Invalid JSON response: ${method} ${path}\n`
    );
    throw new Error("Mnemo Cortex returned an invalid response.");
  }

  return data;
}

// ── Health check on startup ────────────────────────────────────

let mnemoHealthy = false;

async function checkHealth() {
  try {
    const h = await mnemoRequest("GET", "/health");
    if (h.status === "ok") {
      mnemoHealthy = true;
      process.stderr.write(
        `[mnemo-mcp] Connected to Mnemo Cortex (${h.memory_entries} memories, share: ${shareMode})\n`
      );
    }
  } catch {
    process.stderr.write(
      `[mnemo-mcp] WARNING: Mnemo Cortex not reachable. Tools will retry on each call.\n`
    );
  }
}

async function ensureHealth() {
  if (!mnemoHealthy) {
    try {
      const h = await mnemoRequest("GET", "/health");
      if (h.status === "ok") {
        mnemoHealthy = true;
        process.stderr.write(
          `[mnemo-mcp] Mnemo Cortex reconnected (${h.memory_entries} memories)\n`
        );
      }
    } catch {
      throw new Error(
        "Mnemo Cortex is not connected. It may be down or unreachable."
      );
    }
  }
}

// ── Format memory chunks for display ───────────────────────────

// The /context API doesn't surface agent_id in chunks today, but we
// always write session IDs as `${AGENT_ID}-YYYY-MM-DD-HH-MM-SS`, so we
// can recover the agent from the `source` string. Mem0 chunks and
// non-conforming sessions fall through and stay "?".
function inferAgent(source) {
  if (!source) return null;
  // session:cc-2026-04-27-19-37-28  → "cc"
  // session:lmstudio-igor2-2026-04-27-...  → "lmstudio-igor2"
  const m = String(source).match(
    /^session:(.+?)-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}/
  );
  if (m) return m[1];
  // session:dream-2026-04-25  → "dream"
  const d = String(source).match(/^session:(.+?)-\d{4}-\d{2}-\d{2}$/);
  if (d) return d[1];
  if (String(source).startsWith("mem0:")) return "mem0";
  return null;
}

function formatChunks(chunks, showAgent) {
  if (!chunks || chunks.length === 0) return "No memories found.";

  const parts = [];
  let chars = 0;
  let included = 0;

  for (const c of chunks) {
    const rel = (c.relevance || 0).toFixed(2);
    const tier = c.cache_tier || "?";
    const agentTag = c.agent_id || inferAgent(c.source) || "?";
    // v3 provenance / decay surfacing — keep concise; full structured
    // data is still in the JSON tool result for programmatic callers.
    const provBits = [];
    if (c.category) provBits.push(`category=${c.category}`);
    if (c.provenance_source) provBits.push(`source=${c.provenance_source}`);
    if (typeof c.age_days === "number") {
      provBits.push(`age=${Math.round(c.age_days)}d`);
    }
    const provSuffix = provBits.length ? ` ${provBits.join(" ")}` : "";
    const header = showAgent
      ? `[${tier}] agent=${agentTag} (relevance: ${rel})${provSuffix}`
      : `[${tier}] (relevance: ${rel})${provSuffix}`;
    // Stale-warning banner — agents under context pressure may miss
    // the structured stale_warning field. Banner makes it eye-level.
    let warningBanner = "";
    if (c.stale_warning && c.stale_warning.message) {
      const sev = (c.stale_warning.severity || "warn").toUpperCase();
      warningBanner = `\n⚠️ ${sev}: ${c.stale_warning.message}`;
    }
    const block = `### ${header}${warningBanner}\n${c.content}`;

    if (chars + block.length > MAX_RESPONSE_CHARS && included > 0) {
      const remaining = chunks.length - included;
      parts.push(
        `[Results capped — ${remaining} more memories matched. Narrow your query for more detail.]`
      );
      break;
    }

    parts.push(block);
    chars += block.length;
    included++;
  }

  return parts.join("\n\n");
}

// ── Trajectory recipe formatter ────────────────────────────────
// Render one trajectory record as a readable, numbered recipe.

function formatTrajectory(t) {
  const sim =
    t._score && typeof t._score.similarity === "number"
      ? ` (similarity: ${t._score.similarity.toFixed(2)})`
      : "";
  const lines = [
    `### ${t.task_type || "?"} — rating ${t.rating ?? "?"}/5${sim}`,
    `Goal: ${t.task_description || ""}`,
  ];
  const steps = Array.isArray(t.steps) ? t.steps : [];
  if (steps.length) {
    lines.push("Steps:");
    steps.forEach((s, i) => {
      const tool = s.tool_used ? ` [${s.tool_used}]` : "";
      const res = s.result_summary ? ` → ${s.result_summary}` : "";
      lines.push(`  ${i + 1}. ${s.action || ""}${tool}${res}`);
    });
  }
  if (t.outcome) lines.push(`Outcome: ${t.outcome}`);
  return lines.join("\n");
}

// ── Nudge system — remind the agent to save ────────────────────
// Counts non-save tool calls. After SAVE_REMINDER_THRESHOLD calls
// without a manual save, append a reminder to subsequent tool
// responses until the agent calls mnemo_save.

const SAVE_REMINDER_THRESHOLD = 20;
let toolCallCount = 0;
let sessionStartTime = null;
let sessionId = null;

// Session timestamps use host-local time, not UTC, so the date portion
// matches every other timestamp the agents write (active.md, brain commit
// messages, kickstart files). UTC `toISOString()` rolls the date over at
// 17:00 PT, producing session IDs dated "tomorrow" while the rest of the
// brain still says "today".
function localTimestamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-` +
         `${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
}

function localDateOnly() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function nudgeCheck() {
  if (toolCallCount >= SAVE_REMINDER_THRESHOLD) {
    return `\n\n---\n⚠️ **Memory nudge:** ${toolCallCount} tool calls without a save. Call \`mnemo_save\` with a summary before this context is lost.`;
  }
  return null;
}

function trackCall() {
  toolCallCount++;
}

function trackSave() {
  toolCallCount = 0;
}

// ── Auto-capture — ring buffer + periodic flush to /writeback ──
// Captures recall/save/read activity into Mnemo as background
// "what did the agent touch?" trail. Flushes when the buffer hits
// BUFFER_FLUSH_SIZE entries or after BUFFER_FLUSH_IDLE_MS idle.

const BUFFER_FLUSH_SIZE = 8;
const BUFFER_FLUSH_IDLE_MS = 120_000;
// Cap on re-queued entries during a /writeback outage so a long server-side
// outage can't grow this buffer unboundedly (keeps the most recent activity).
const MAX_BUFFER_BACKLOG = 200;
const captureBuffer = [];
let flushTimer = null;

const TOOL_CAPTURE = {
  mnemo_recall: "summary",
  mnemo_search: "summary",
  // A manual mnemo_save already persists the memory via its own handler.
  // Capturing it here re-wrote the same fact as a duplicate [AUTO-CAPTURE]
  // chunk, diluting recall (~5% of CC's store was these echoes). Skip it.
  mnemo_save: "skip",
  mnemo_share: "skip",
  agent_startup: "skip",
  opie_startup: "skip",
  read_brain_file: "summary",
  list_brain_files: "skip",
  write_brain_file: "full",
  session_end: "drain",
  wiki_search: "summary",
  wiki_read: "summary",
  wiki_index: "skip",
  passport_get_user_context: "skip",
  passport_observe_behavior: "skip",
  passport_list_pending_observations: "skip",
  passport_promote_observation: "skip",
  passport_forget_or_override: "skip",
};

function captureCall(toolName, summary) {
  trackCall();

  const policy = TOOL_CAPTURE[toolName] || "skip";
  if (policy === "skip") return;

  captureBuffer.push({
    tool: toolName,
    summary,
    ts: new Date().toISOString(),
  });

  if (flushTimer) clearTimeout(flushTimer);
  flushTimer = setTimeout(() => flushBuffer(), BUFFER_FLUSH_IDLE_MS);

  if (captureBuffer.length >= BUFFER_FLUSH_SIZE) {
    flushBuffer();
  }
}

async function flushBuffer() {
  if (captureBuffer.length === 0) return;
  if (flushTimer) clearTimeout(flushTimer);
  flushTimer = null;

  const entries = captureBuffer.splice(0);
  const narrative = entries.map((e) => `- [${e.tool}] ${e.summary}`).join("\n");
  const keyFacts = entries
    .filter((e) => TOOL_CAPTURE[e.tool] === "full")
    .map((e) => e.summary.slice(0, 100));

  const sid = sessionId || `${AGENT_ID}-auto-${Date.now()}`;

  try {
    await mnemoRequest("POST", "/writeback", {
      session_id: sid,
      summary: `[AUTO-CAPTURE] ${entries.length} tool calls:\n${narrative}`,
      key_facts: keyFacts.length > 0 ? keyFacts : ["auto_capture_flush"],
      projects_referenced: [],
      decisions_made: [],
      agent_id: AGENT_ID,
      // Mnemo v3 — mechanical ambient capture, not agent inference. Tag
      // accordingly so default recalls don't drown in tool-call narratives.
      source: "tool",
      category: "session_log",
    });
  } catch (err) {
    // Re-queue instead of dropping the trail (this was a silent-loss bug: the
    // spliced-out batch was discarded on failure). Put the failed batch back at
    // the front to preserve order, cap the backlog so a prolonged /writeback
    // outage can't grow memory unboundedly, and self-schedule a retry so
    // recovery doesn't depend on the next captureCall arriving.
    process.stderr.write(`[auto-capture] flush failed (will retry): ${err.message}\n`);
    captureBuffer.unshift(...entries);
    if (captureBuffer.length > MAX_BUFFER_BACKLOG) {
      captureBuffer.splice(0, captureBuffer.length - MAX_BUFFER_BACKLOG);
    }
    if (!flushTimer) flushTimer = setTimeout(() => flushBuffer(), BUFFER_FLUSH_IDLE_MS);
  }
}

// Graceful shutdown — drain the buffer before exit
process.on("SIGTERM", async () => {
  if (captureBuffer.length > 0) await flushBuffer();
  process.exit(0);
});
process.on("SIGINT", async () => {
  if (captureBuffer.length > 0) await flushBuffer();
  process.exit(0);
});

// Diagnostic logging — capture WHY the bridge dies. Without these,
// silent exits leave no trace in Claude Desktop's MCP log.
process.on("uncaughtException", (err) => {
  process.stderr.write(`[mnemo-mcp] FATAL uncaughtException: ${err?.stack || err}\n`);
  process.exit(1);
});
process.on("unhandledRejection", (reason) => {
  process.stderr.write(`[mnemo-mcp] unhandledRejection: ${reason?.stack || reason}\n`);
});
process.on("exit", (code) => {
  process.stderr.write(`[mnemo-mcp] process exiting code=${code}\n`);
});
process.on("SIGHUP", () => {
  process.stderr.write(`[mnemo-mcp] SIGHUP received — exiting\n`);
  process.exit(0);
});
process.on("SIGPIPE", () => {
  process.stderr.write(`[mnemo-mcp] SIGPIPE received — exiting\n`);
  process.exit(0);
});
process.stdin.on("end", () => {
  process.stderr.write(`[mnemo-mcp] stdin EOF — parent disconnected\n`);
});

// ── MCP Server ─────────────────────────────────────────────────

// Version single-sourced from package.json — hardcoded copies here and in
// package.json drifted three ways (4.1.1 / 2.15.1 / 2.10.0) before this.
const BRIDGE_VERSION = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8")
).version;

const server = new McpServer({
  name: "mnemo-cortex",
  version: BRIDGE_VERSION,
});

// ── Developer Dump (v2.9.0, Mnemo v4 Phase 1) ──────────────────
// Wraps every tool handler with JSONL capture when MNEMO_DUMP=on.
// Zero overhead when off. Monkey-patches server.registerTool so all
// existing and future tool registrations are covered with one diff.
// Output: ~/.mnemo-cortex/dumps/<agent_id>/<YYYY-MM-DD>.jsonl
const dump = new DumpWriter(AGENT_ID);
const harnessToolGate = createHarnessToolGate(process.env.HARNESS_ENABLED_TOOLS);
const _origRegisterTool = server.registerTool.bind(server);
server.registerTool = (name, schema, handler) =>
  harnessToolGate.register(
    _origRegisterTool,
    name,
    schema,
    dump.wrap(name, handler)
  );

if (dump.enabled) {
  process.stderr.write(
    `[mnemo-mcp] Developer Dump ON — writing to ${dump.dir}/${AGENT_ID}/<date>.jsonl\n`
  );
}

// ── Tool: mnemo_recall ─────────────────────────────────────────
// Semantic recall within this agent's own memories.

server.registerTool(
  "mnemo_recall",
  {
    description: `Recall memories from Mnemo Cortex for the current agent (${AGENT_ID}). Returns semantically relevant chunks from past sessions. Each chunk may carry a structured stale_warning when it exceeds its category's decay threshold — verify with a tool call before acting on stale topology/current_state facts.`,
    inputSchema: {
    query: z
      .string()
      .max(10000)
      .describe("What to search for in memory"),
    max_results: z
      .number()
      .int()
      .min(1)
      .max(20)
      .optional()
      .describe("Maximum number of memories to return (default: 3)"),
    source: z
      .enum(["user", "tool", "inferred", "brain", "migrated"])
      .optional()
      .describe(
        "Restrict to one provenance source. Use 'user' or 'tool' for highest-confidence facts."
      ),
    category: z
      .enum([
        "topology", "current_state", "doctrine", "incident",
        "identity", "relationship", "decision", "idea", "session_log", "unknown",
      ])
      .optional()
      .describe("Restrict to a single category."),
    exclude_categories: z
      .array(z.string())
      .optional()
      .describe(
        "Categories to drop from results. Defaults to ['session_log']. Pass [] to include everything."
      ),
    exclude_stale: z
      .boolean()
      .optional()
      .describe("Drop topology records past 1.5x their warn threshold."),
    max_age_days: z
      .number()
      .int()
      .min(1)
      .optional()
      .describe("Hard upper bound on record age in days."),
    mode: z
      .enum(["focus", "explore"])
      .optional()
      .describe(
        "Recall lens. 'focus' (default): best match wins. 'explore': the serendipity lens — " +
        "what does this remind the store of; prefers the adjacent similarity band, ignores recency, " +
        "favors rarely-recalled memories. Use for brainstorming and idea recall."
      ),
  },
    annotations: { "title": 'Recall Memories', "readOnlyHint": true, "idempotentHint": true, "openWorldHint": true },
  },
  async ({ query, max_results, source, category, exclude_categories, exclude_stale, max_age_days, mode }) => {
    try {
      await ensureHealth();
      const requestBody = {
        prompt: query,
        agent_id: AGENT_ID,
        max_results: max_results || 3,
      };
      if (source !== undefined) requestBody.source = source;
      if (category !== undefined) requestBody.category = category;
      if (exclude_categories !== undefined) requestBody.exclude_categories = exclude_categories;
      if (exclude_stale !== undefined) requestBody.exclude_stale = exclude_stale;
      if (max_age_days !== undefined) requestBody.max_age_days = max_age_days;
      if (mode !== undefined) requestBody.mode = mode;
      const data = await mnemoRequest("POST", "/context", requestBody);

      const chunks = data.chunks || [];
      captureCall(
        "mnemo_recall",
        `${chunks.length} memories about: ${query.slice(0, 80)}`
      );
      const text = formatChunks(chunks, false);
      const count = data.total_found || chunks.length;
      const body = count > 0 ? `Found ${count} memories:\n\n${text}` : text;

      return {
        content: [
          { type: "text", text: body + (nudgeCheck() || "") },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Recall error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_search ─────────────────────────────────────────
// Cross-agent search. Gated by share mode.

server.registerTool(
  "mnemo_search",
  {
    description: "Search memories in Mnemo Cortex. By default, searches only your own memories. Use mnemo_share to enable cross-agent search for this session. Returns structured stale_warning on results past their category's warn threshold — verify before acting on stale topology facts.",
    inputSchema: {
    query: z
      .string()
      .max(10000)
      .describe("What to search for"),
    agent_id: z
      .string()
      .optional()
      .describe(
        "Filter to a specific agent (rocky, cc, opie). Only works when cross-agent sharing is enabled. Omit for all."
      ),
    max_results: z
      .number()
      .int()
      .min(1)
      .max(20)
      .optional()
      .describe("Maximum number of memories to return (default: 3)"),
    source: z
      .enum(["user", "tool", "inferred", "brain", "migrated"])
      .optional()
      .describe(
        "Restrict to one provenance source. Use 'user' or 'tool' for highest-confidence facts."
      ),
    category: z
      .enum([
        "topology", "current_state", "doctrine", "incident",
        "identity", "relationship", "decision", "idea", "session_log", "unknown",
      ])
      .optional()
      .describe("Restrict to a single category."),
    exclude_categories: z
      .array(z.string())
      .optional()
      .describe(
        "Categories to drop from results. Defaults to ['session_log']. Pass [] to include everything."
      ),
    exclude_stale: z
      .boolean()
      .optional()
      .describe("Drop topology records past 1.5x their warn threshold."),
    max_age_days: z
      .number()
      .int()
      .min(1)
      .optional()
      .describe("Hard upper bound on record age in days."),
  },
    annotations: { "title": 'Search Memories Across Agents', "readOnlyHint": true, "idempotentHint": true, "openWorldHint": true },
  },
  async ({ query, agent_id, max_results, source, category, exclude_categories, exclude_stale, max_age_days }) => {
    try {
      await ensureHealth();
      const body = {
        prompt: query,
        max_results: max_results || 3,
      };

      if (sessionShareActive) {
        if (agent_id) body.agent_id = agent_id;
      } else {
        body.agent_id = AGENT_ID;
      }
      if (source !== undefined) body.source = source;
      if (category !== undefined) body.category = category;
      if (exclude_categories !== undefined) body.exclude_categories = exclude_categories;
      if (exclude_stale !== undefined) body.exclude_stale = exclude_stale;
      if (max_age_days !== undefined) body.max_age_days = max_age_days;

      const data = await mnemoRequest("POST", "/context", body);
      const chunks = data.chunks || [];
      captureCall(
        "mnemo_search",
        `cross-agent (${agent_id || (sessionShareActive ? "all" : AGENT_ID)}): ${query.slice(0, 80)} → ${chunks.length} results`
      );
      const text = formatChunks(chunks, sessionShareActive);
      const count = data.total_found || chunks.length;

      let prefix = "";
      if (!sessionShareActive) {
        prefix =
          "(Restricted to your own memories. Use mnemo_share to enable cross-agent search.)\n\n";
      }

      const out =
        count > 0 ? `${prefix}Found ${count} memories:\n\n${text}` : `${prefix}${text}`;

      return {
        content: [
          { type: "text", text: out + (nudgeCheck() || "") },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Search error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_save ───────────────────────────────────────────
// Write a memory to Mnemo Cortex. Always writes to this agent's slot.

server.registerTool(
  "mnemo_save",
  {
    description: "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered. Optional v3 provenance fields (source, category, additional_tags) let you mark how the fact was learned and how it should decay — when omitted, a regex auto-suggester picks a category from the content.",
    inputSchema: {
    summary: z
      .string()
      .max(10000)
      .describe("Summary of what happened or what to remember"),
    key_facts: z
      .array(z.string().max(1000))
      .optional()
      .describe("List of key facts to store (one fact per item)"),
    session_id: z
      .string()
      .optional()
      .describe("Session identifier. Auto-generated if omitted."),
    source: z
      .enum(["user", "tool", "inferred", "brain", "migrated"])
      .optional()
      .describe(
        "Where this fact came from. Defaults to 'inferred'. Use 'user' when Guy stated it directly, 'tool' for deterministic outputs, 'brain' when pulled from a brain file."
      ),
    category: z
      .enum([
        "topology", "current_state", "doctrine", "incident",
        "identity", "relationship", "decision", "idea", "session_log", "unknown",
      ])
      .optional()
      .describe(
        "Drives decay behavior. Omit to let the regex auto-suggester choose — the response will tell you what it picked so you can override on the next save."
      ),
    additional_tags: z
      .array(z.string().max(64))
      .optional()
      .describe("Free-form human-readable tags for search."),
  },
    annotations: { "title": 'Save Memory', "readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": true },
  },
  async ({ summary, key_facts, session_id, source, category, additional_tags }) => {
    captureCall("mnemo_save", summary.slice(0, 150));
    trackSave();
    try {
      await ensureHealth();
      const sid =
        session_id ||
        sessionId ||
        `${AGENT_ID}-${localTimestamp()}`;

      const body = {
        session_id: sid,
        summary,
        key_facts: key_facts || [],
        projects_referenced: [],
        decisions_made: [],
        agent_id: AGENT_ID,
      };
      if (source !== undefined) body.source = source;
      if (category !== undefined) body.category = category;
      if (additional_tags !== undefined) body.additional_tags = additional_tags;

      const data = await mnemoRequest("POST", "/writeback", body);

      // Surface what the server actually stored so the agent can learn.
      const lines = [
        "Saved to Mnemo Cortex.",
        `  memory_id: ${data.memory_id || "ok"}`,
        `  session:   ${sid}`,
        `  agent:     ${AGENT_ID}`,
      ];
      if (data.source_used) lines.push(`  source:    ${data.source_used}`);
      if (data.category_used) lines.push(`  category:  ${data.category_used}`);
      if (data.category_suggested && data.category_match_keywords && data.category_match_keywords.length) {
        lines.push(
          `  (auto-suggested from keywords: ${data.category_match_keywords.join(", ")})`
        );
      }
      return {
        content: [{ type: "text", text: lines.join("\n") }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Save error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_save_trajectory ────────────────────────────────
// Capture a proven task recipe AFTER a task succeeds (v4.5).

server.registerTool(
  "mnemo_save_trajectory",
  {
    description:
      "Save a proven task recipe to Mnemo Cortex AFTER you complete a task well, so you (or another session) can recall the working approach before a similar task. Capture the ordered steps you actually took, the outcome, and an honest 1–5 self-rating. Use after a non-trivial task that went well (or instructively badly — a low rating still teaches).",
    inputSchema: {
      task_type: z
        .string()
        .min(1)
        .max(128)
        .describe("Category tag, e.g. shopify_fix, bus_debug, security_triage, file_migration"),
      task_description: z
        .string()
        .min(1)
        .max(10000)
        .describe("The goal — what you set out to do"),
      steps: z
        .array(
          z.object({
            action: z.string().describe("What you did at this step"),
            tool_used: z.string().optional().describe("Tool/command used, if any"),
            args: z.record(z.string(), z.any()).optional().describe("Arguments, if any"),
            result_summary: z.string().optional().describe("What the step produced"),
          })
        )
        .min(1)
        .describe("Ordered list of the steps you took"),
      outcome: z.string().min(1).max(10000).describe("The final result"),
      rating: z
        .number()
        .int()
        .min(1)
        .max(5)
        .describe("Honest self-assessment 1–5 (5 = clean success)"),
      token_cost: z.number().int().min(0).optional(),
      model: z.string().optional(),
      duration_seconds: z.number().int().min(0).optional(),
    },
    annotations: { title: "Save Trajectory", readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
  },
  async ({ task_type, task_description, steps, outcome, rating, token_cost, model, duration_seconds }) => {
    captureCall("mnemo_save_trajectory", `${task_type}: ${task_description.slice(0, 100)}`);
    try {
      await ensureHealth();
      const body = {
        agent_id: AGENT_ID,
        task_type,
        task_description,
        steps: steps.map((s) => ({
          action: s.action,
          tool_used: s.tool_used,
          args: s.args,
          result_summary: s.result_summary || "",
        })),
        outcome,
        rating,
      };
      if (token_cost !== undefined) body.token_cost = token_cost;
      if (model !== undefined) body.model = model;
      if (duration_seconds !== undefined) body.duration_seconds = duration_seconds;

      const data = await mnemoRequest("POST", "/trajectory/save", body);
      const lines = [
        "Trajectory saved to Mnemo Cortex.",
        `  trajectory_id: ${data.trajectory_id || "ok"}`,
        `  task_type:     ${task_type}`,
        `  rating:        ${rating}/5`,
        `  agent:         ${AGENT_ID}`,
      ];
      if (typeof data.total_for_agent === "number") {
        lines.push(`  (${data.total_for_agent} trajectories now stored for ${AGENT_ID})`);
      }
      return { content: [{ type: "text", text: lines.join("\n") }] };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Save trajectory error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_recall_trajectory ──────────────────────────────
// Recall proven task recipes BEFORE a similar task (v4.5).

server.registerTool(
  "mnemo_recall_trajectory",
  {
    description: `Recall proven task recipes for the current agent (${AGENT_ID}) BEFORE starting a task. Returns past trajectories ranked by semantic similarity, then rating, then recency — each with its full step sequence (the proven recipe). Call this when you're about to do something you may have done before.`,
    inputSchema: {
      query: z
        .string()
        .min(1)
        .max(10000)
        .describe("Describe what you're about to do"),
      task_type: z.string().optional().describe("Filter by category tag"),
      min_rating: z
        .number()
        .int()
        .min(1)
        .max(5)
        .optional()
        .describe("Quality threshold, default 3 (excludes ratings below this)"),
      max_results: z.number().int().min(1).max(20).optional().describe("Default 3"),
    },
    annotations: { title: "Recall Trajectory", readOnlyHint: true, idempotentHint: true, openWorldHint: true },
  },
  async ({ query, task_type, min_rating, max_results }) => {
    try {
      await ensureHealth();
      const body = { query, agent_id: AGENT_ID };
      if (task_type !== undefined) body.task_type = task_type;
      if (min_rating !== undefined) body.min_rating = min_rating;
      if (max_results !== undefined) body.max_results = max_results;

      const data = await mnemoRequest("POST", "/trajectory/recall", body);
      const trajs = data.trajectories || [];
      captureCall("mnemo_recall_trajectory", `${trajs.length} recipes for: ${query.slice(0, 80)}`);
      if (trajs.length === 0) {
        return {
          content: [
            { type: "text", text: "No matching trajectories found. (Nothing saved yet, or none above the rating threshold.)" },
          ],
        };
      }
      const text = trajs.map(formatTrajectory).join("\n\n---\n\n");
      return {
        content: [{ type: "text", text: `Found ${trajs.length} trajectory recipe(s):\n\n${text}` }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Recall trajectory error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_share ──────────────────────────────────────────
// Toggle cross-agent memory sharing for this session.

server.registerTool(
  "mnemo_share",
  {
    description: "Toggle cross-agent memory sharing for this session. When on, mnemo_search can read memories from all agents. When off, search is limited to this agent only.",
    annotations: { "title": 'Toggle Cross-Agent Sharing', "readOnlyHint": false, "idempotentHint": false },
  },
  async () => {
    if (shareMode === "never") {
      return {
        content: [
          {
            type: "text",
            text: "Cross-agent sharing is disabled for this agent. This cannot be overridden.",
          },
        ],
      };
    }
    if (shareMode === "always") {
      return {
        content: [
          {
            type: "text",
            text: "Cross-agent sharing is always on for this agent. Toggle not needed.",
          },
        ],
      };
    }
    sessionShareActive = !sessionShareActive;
    return {
      content: [
        {
          type: "text",
          text: `Cross-agent sharing is now ${sessionShareActive ? "ON" : "OFF"} for this session.`,
        },
      ],
    };
  }
);

// ── Brain-lane + session tools (conditional) ──────────────────
// Only register if BRAIN_DIR exists. New users without a brain
// checkout get a clean memory bridge. Sparks operators get the
// full kit automatically.

if (BRAIN_AVAILABLE) {

// ── Shared startup helper ──────────────────────────────────────
// Used by both agent_startup (neutral, agent-aware) and the legacy
// opie_startup alias. The two tools differ only in (1) which lane
// filename to load and (2) the identity-header text prepended to
// the response. Everything else — git pull, cross-agent docs,
// Mnemo context, dream brief, session writeback — is identical
// and agent-agnostic.

// Brain files (esp. the session lane + active.md) grow unbounded over time —
// CC's lane hit ~572 KB / 4,656 lines, which blew past the MCP tool-result cap
// and made agent_startup unreadable (audit finding 3.3). v2.16.0: each boot
// section now carries its own byte budget (see boot-budget.js) so the TOTAL
// boot block stays under ~45KB and lands inline. Files are newest-first /
// priority-first, so the top slice is the right slice.
const STARTUP_FILE_CAP = 40_000; // fallback for files without a named budget
async function readBrainCapped(path, cap = STARTUP_FILE_CAP) {
  const content = await readFile(path, "utf-8");
  return capSection(
    content,
    cap,
    `newest-first lanes → most-recent kept; active.md → highest-priority kept; ` +
      `use read_brain_file for the full file`
  );
}

async function _runStartup({ effectiveAgentId, identityHeader, laneCandidates }) {
  sessionStartTime = new Date().toISOString();
  sessionId = `${effectiveAgentId}-${localTimestamp()}`;
  toolCallCount = 0;
  captureBuffer.length = 0;
  if (flushTimer) clearTimeout(flushTimer);
  flushTimer = null;

  // Pull the brain repo so we read the freshest cross-agent state,
  // not whatever was last on disk. Best-effort — if pull fails (no
  // network, dirty tree, etc.) we keep going with local files.
  // The brain dir is often a SUBDIR of its repo — sparks-brain/brain, or
  // the mnemo-plan default ~/mnemo-plan/brain — where .git lives at the repo
  // root, not inside BRAIN_DIR. Looking for join(BRAIN_DIR, ".git") missed
  // that and silently skipped the pull. Detect the work tree the way git
  // itself does (walking up the tree); git pull then runs fine from the
  // subdir (it's a repo-level op regardless of cwd).
  let pullStatus = "skipped (not a git repo)";
  let insideWorkTree = false;
  try {
    insideWorkTree =
      execSync("git rev-parse --is-inside-work-tree", {
        cwd: BRAIN_DIR,
        encoding: "utf-8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim() === "true";
  } catch {
    // Not a git repo (or git unavailable) — keep the "skipped" status.
  }
  if (insideWorkTree) {
    try {
      const out = execSync("git pull --ff-only", {
        cwd: BRAIN_DIR,
        encoding: "utf-8",
        stdio: ["ignore", "pipe", "pipe"],
      }).trim();
      pullStatus = out.split("\n")[0] || "OK";
    } catch (e) {
      pullStatus = `FAILED (${e.message.split("\n")[0]})`;
    }
  }

  try {
    const parts = [];

    // Try lane filenames in order; first one that exists wins.
    let laneLoaded = null;
    for (const candidate of laneCandidates) {
      try {
        const brain = await readBrainCapped(join(BRAIN_DIR, candidate), STARTUP_BUDGETS.lane);
        parts.push(`# YOUR BRAIN LANE (${candidate})\n\n` + brain);
        laneLoaded = candidate;
        break;
      } catch {
        // try next candidate
      }
    }
    if (!laneLoaded) {
      parts.push(
        `# BRAIN LANE NOT FOUND\n` +
        `No lane file found for agent_id "${effectiveAgentId}". ` +
        `Looked for: ${laneCandidates.join(", ")} in ${BRAIN_DIR}.\n` +
        `Create one via write_brain_file when ready.`
      );
    }

    // v2.15.x: lane-freshness report, EVERY boot (Guy, 2026-07-05). The
    // Lane Protocol's "update your own lane file" step lived only in tool
    // descriptions and was skipped by every agent except cc (opie.md found
    // 43 days stale). The boot block now leads with the lane's git age —
    // a gentle line when current, a warning banner once the last session
    // is missing from it.
    if (laneLoaded && insideWorkTree) {
      try {
        const ct = execFileSync("git", ["log", "-1", "--format=%ct", "--", laneLoaded], {
          cwd: BRAIN_DIR,
          encoding: "utf-8",
          stdio: ["ignore", "pipe", "ignore"],
        }).trim();
        if (ct) {
          const ageDays = (Date.now() / 1000 - Number(ct)) / 86400;
          const when = new Date(Number(ct) * 1000).toISOString().slice(0, 10);
          const rounded = Math.round(ageDays);
          if (ageDays > LANE_BEHIND_DAYS) {
            parts.unshift(
              `# ⚠️ YOUR LANE FILE IS BEHIND\n\n` +
              `${laneLoaded} last commit: ${when} (${rounded} day${rounded === 1 ? "" : "s"} ago). ` +
              `The sessions since then are MISSING from your lane, and the other agents are ` +
              `coordinating off that old reality. Before this session ends: update it ` +
              `(write_brain_file — date bump + what changed since) and call session_end to commit.`
            );
          } else {
            parts.unshift(
              `# LANE FRESHNESS\n\n` +
              `${laneLoaded} last commit: ${when} — current. Keep the streak: update your ` +
              `lane at the end of THIS session too (write_brain_file + session_end). ` +
              `Every agent, every session.`
            );
          }
        }
      } catch {
        // a failed git log is never worth breaking a boot
      }
    }

    // CLAUDE.md is the cross-agent operating doc — Lane Protocol
    // applied to this brain. Loaded BEFORE active/people/doctrines so
    // its session ritual frames everything else.
    for (const file of ["CLAUDE.md", "active.md", "people.md", "doctrines.md"]) {
      try {
        const content = await readBrainCapped(join(BRAIN_DIR, file), STARTUP_BUDGETS[file]);
        parts.push(`# ${file.toUpperCase()}\n\n` + content);
      } catch {
        // skip if missing
      }
    }

    try {
      const data = await mnemoRequest("POST", "/context", {
        prompt: "recent session summary, current projects, what happened last",
        agent_id: effectiveAgentId,
        max_results: 3,
      });
      const chunks = data.chunks || [];
      if (chunks.length > 0) {
        const mnemoText = chunks
          .map((c) => {
            const tier = c.cache_tier || "?";
            return `### [${tier}]\n${c.content}`;
          })
          .join("\n\n");
        parts.push(
          "# RECENT MNEMO CONTEXT\n\n" +
            capSection(mnemoText, STARTUP_BUDGETS.mnemo, "use mnemo_recall to pull more")
        );
      }
    } catch (e) {
      parts.push("# MNEMO ERROR\nCould not reach Mnemo Cortex: " + e.message);
    }

    try {
      // v2.14.0: dreams are written on the Cortex host, which is not
      // necessarily this machine — ask the server first, keep the local
      // DREAM_DIR read as an offline fallback. 48h freshness gate on both.
      let brief = null;
      try {
        const d = await mnemoRequest("GET", "/dream/latest");
        if (d && d.content && d.age_hours < 48) {
          brief = { ageH: Math.round(d.age_hours), content: d.content };
        }
      } catch {
        // server predates /dream/latest or is unreachable — try local disk
      }
      if (!brief) {
        const dreamFiles = (await readdir(DREAM_DIR))
          .filter((f) => f.endsWith(".md"))
          .sort()
          .reverse();
        if (dreamFiles.length > 0) {
          const latestDream = join(DREAM_DIR, dreamFiles[0]);
          const st = await stat(latestDream);
          const dreamAge = (Date.now() - st.mtimeMs) / 3600000;
          if (dreamAge < 48) {
            brief = {
              ageH: Math.round(dreamAge),
              content: await readFile(latestDream, "utf-8"),
            };
          }
        }
      }
      if (brief) {
        parts.push(
          `# DREAM BRIEF (cross-agent overnight synthesis, ${brief.ageH}h ago)\n\n` +
            capSection(
              brief.content,
              STARTUP_BUDGETS.dream,
              "full brief via GET /dream/latest on the Cortex host"
            )
        );
      }
    } catch {
      // dreams are supplementary
    }

    try {
      await mnemoRequest("POST", "/writeback", {
        session_id: sessionId,
        summary: `${effectiveAgentId} session started at ${sessionStartTime}. Brain lane loaded${laneLoaded ? ` (${laneLoaded})` : ""}.`,
        key_facts: ["session_start"],
        projects_referenced: [],
        decisions_made: [],
        agent_id: effectiveAgentId,
        // Mnemo v3 — deterministic startup marker, not agent inference.
        source: "tool",
        category: "session_log",
        additional_tags: ["session_start"],
      });
    } catch {
      // session-start marker is best-effort
    }

    const header = identityHeader({ pullStatus, laneLoaded, sessionId });

    return {
      content: [{ type: "text", text: header + parts.join("\n\n---\n\n") }],
    };
  } catch (err) {
    return {
      content: [{ type: "text", text: `Startup error: ${err.message}` }],
      isError: true,
    };
  }
}

// ── Tool: agent_startup ────────────────────────────────────────
// Neutral session-boot tool. Loads the lane file matching
// MNEMO_AGENT_ID (or its `-session.md` variant), cross-agent docs,
// recent Mnemo memories, and the latest dream brief. Returns an
// agent-neutral header + content. Identity stays in the agent's
// system prompt (SOUL.md / instruction); this tool just provides
// continuity. Use this for any agent — Rocky, CC, BW, you, them.

server.registerTool(
  "agent_startup",
  {
    description: "CALL THIS FIRST in every new conversation. Loads your brain lane (named after your MNEMO_AGENT_ID env, e.g. rocky.md / cc-session.md / opie.md), the cross-agent operating docs (CLAUDE.md, active.md, people.md, doctrines.md), recent Mnemo memories tagged to your agent_id, and the latest dream brief. Returns an agent-neutral session-boot block — your identity comes from your system prompt, this gives you continuity.",
    annotations: { "title": 'Agent session boot', "readOnlyHint": false, "idempotentHint": false, "openWorldHint": true },
  },
  () => _runStartup({
    effectiveAgentId: AGENT_ID,
    laneCandidates: [`${AGENT_ID}.md`, `${AGENT_ID}-session.md`],
    identityHeader: ({ pullStatus, laneLoaded, sessionId }) =>
      `# AGENT BOOT — ${AGENT_ID}

This is your session boot from the Mnemo MCP bridge. Your **lane file** below is your continuity; your **system prompt** establishes who you are and how you work. Lane Protocol applies if your brain follows it (read CLAUDE.md below for the six-step session ritual).

- **Brain pull:** ${pullStatus}
- **Lane file loaded:** ${laneLoaded || "(none — see warning below)"}
- **Session ID:** ${sessionId}
- **Auto-capture:** ACTIVE — every ${BUFFER_FLUSH_SIZE} tool calls or 2 min idle, summary flushes to Mnemo. Reminder after ${SAVE_REMINDER_THRESHOLD} calls without a manual save.
- **Today:** ${new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}

**Save protocol:** call \`mnemo_save\` after major decisions, specs, or deliverables. Call \`session_end\` before wrapping up — it flushes auto-capture, saves a summary, and commits your brain lane. Auto-capture is the safety net; manual saves are the high-signal memory.

`,
  })
);

// ── Tool: opie_startup (DEPRECATED — kept as alias for back-compat) ──
// Original Opie-specific boot tool. New installs should use
// agent_startup (which respects MNEMO_AGENT_ID). This alias forces
// agent_id="opie" and loads opie.md regardless of env, preserving
// existing Opie installs' behavior bit-for-bit. Will be removed in a
// future version once existing Opie configs migrate.

server.registerTool(
  "opie_startup",
  {
    description: "DEPRECATED — use `agent_startup` instead. Legacy alias that loads opie.md as the lane and forces agent_id=opie regardless of MNEMO_AGENT_ID. Kept for back-compat with existing Opie / Claude Desktop installs. Identity lives in opie.md, not in this tool.",
    annotations: { "title": 'Opie startup (deprecated alias)', "readOnlyHint": false, "idempotentHint": false, "openWorldHint": true },
  },
  () => _runStartup({
    effectiveAgentId: "opie",
    laneCandidates: ["opie.md"],
    identityHeader: ({ pullStatus, laneLoaded, sessionId }) =>
      `# OPIE STARTUP (deprecated alias — call \`agent_startup\` instead going forward)

This call loaded the **opie.md** lane and forced agent_id="opie" for back-compat.
Your identity, role, and operating instructions live in opie.md (above) — that's your brain lane,
not the bridge. If you don't have an opie.md, write one with \`write_brain_file\`.

Brain pull: ${pullStatus}.
Lane file: ${laneLoaded ? "loaded above" : "missing — create opie.md to define this agent's role"}.
Today: ${new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}

# SESSION MEMORY
**Auto-capture is ACTIVE.** Every ${BUFFER_FLUSH_SIZE} tool calls (or after 2 minutes idle), a summary
flushes to Mnemo Cortex. The nudge system also reminds you after ${SAVE_REMINDER_THRESHOLD} calls
without a manual save.
Session ID: ${sessionId}

Call \`mnemo_save\` for important decisions, specs, and deliverables — auto-capture records
*what* you used; manual saves record *why* you decided.
Call \`session_end\` before wrapping — flushes auto-capture, persists the summary, commits the brain.

# LANE PROTOCOL

The brain follows the **Lane Protocol** — a six-step session ritual. See CLAUDE.md (loaded above
if present in the brain) for the full convention. Brain pull above: ${pullStatus}.

This call handled steps 1–2 (pull + load your lane + shared docs). Continue:

3. **Read task-specific files** as needed via \`read_brain_file\`.
4. **Work normally.**
5. **Write back what changed** — mark done tasks in shared docs, update your own lane file last.
6. **\`session_end\`** — flushes auto-capture, saves the summary, commits the brain.

Per Lane Protocol: write only to your own lane file (opie.md). Read shared docs and other agents'
lanes; don't write them.
`,
  })
);

// ── Tool: read_brain_file ──────────────────────────────────────

server.registerTool(
  "read_brain_file",
  {
    description: "Read a file from the brain directory ($BRAIN_DIR). Use this to check brain lanes, reference docs, or any .md file in the brain.",
    inputSchema: {
    filename: z
      .string()
      .describe("Filename to read, e.g. 'opie.md', 'active.md', 'stack.md'"),
  },
    annotations: { "title": 'Read Brain File', "readOnlyHint": true, "idempotentHint": true },
  },
  async ({ filename }) => {
    captureCall("read_brain_file", `read ${filename}`);
    try {
      const safe = filename.replace(/[^a-zA-Z0-9._-]/g, "");
      const content = await readFile(join(BRAIN_DIR, safe), "utf-8");
      return {
        content: [{ type: "text", text: content + (nudgeCheck() || "") }],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Error reading ${filename}: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: list_brain_files ─────────────────────────────────────

server.registerTool(
  "list_brain_files",
  {
    description: "List all files in the brain directory ($BRAIN_DIR). Use to discover what brain lanes and reference docs are available.",
    annotations: { "title": 'List Brain Files', "readOnlyHint": true, "idempotentHint": true },
  },
  async () => {
    try {
      const files = await readdir(BRAIN_DIR);
      const mdFiles = files.filter((f) => f.endsWith(".md")).sort();
      return {
        content: [
          {
            type: "text",
            text: `Brain files:\n${mdFiles.map((f) => `- ${f}`).join("\n")}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Error listing brain: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: write_brain_file ─────────────────────────────────────

server.registerTool(
  "write_brain_file",
  {
    description: "Write or update a file in the brain directory ($BRAIN_DIR). Use at session end to update your own lane file. Per the Lane Protocol convention, write only to your own lane (named after MNEMO_AGENT_ID), not other agents' lanes or shared docs.",
    inputSchema: {
    filename: z
      .string()
      .describe("Filename to write, e.g. 'opie.md', 'active.md'"),
    content: z.string().describe("Full file content to write"),
  },
    annotations: { "title": 'Write Brain File', "readOnlyHint": false, "destructiveHint": true, "idempotentHint": true },
  },
  async ({ filename, content }) => {
    captureCall(
      "write_brain_file",
      `wrote ${filename} (${content.length} bytes)`
    );
    try {
      const safe = filename.replace(/[^a-zA-Z0-9._-]/g, "");
      if (["cc-session.md", "CLAUDE.md"].includes(safe)) {
        return {
          content: [
            { type: "text", text: `Refused: ${safe} is not yours to write.` },
          ],
          isError: true,
        };
      }
      await writeFile(join(BRAIN_DIR, safe), content, "utf-8");
      // Commit + push immediately so the write is visible fleet-wide even
      // if this session never reaches session_end (stranded-lane fix).
      const gitStatus = autoCommitBrainFile({
        brainDir: BRAIN_DIR,
        filename: safe,
        agentId: AGENT_ID,
        dateStr: localDateOnly(),
      });
      return {
        content: [
          {
            type: "text",
            text: `Wrote ${safe} (${content.length} bytes); git: ${gitStatus}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Error writing ${filename}: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: session_end ──────────────────────────────────────────
// Drain auto-capture buffer, save final summary, commit + push
// brain lane changes.

server.registerTool(
  "session_end",
  {
    description: "Call this before ending a session. Saves a final summary to Mnemo Cortex and commits brain lane changes. This is your last chance to preserve what happened in this conversation.",
    inputSchema: {
    summary: z
      .string()
      .describe(
        "Final session summary — what was accomplished, decided, and what's next"
      ),
    key_facts: z
      .array(z.string())
      .optional()
      .describe("Key facts to remember from this session"),
  },
    annotations: { "title": 'End Session (Save & Commit)', "readOnlyHint": false, "destructiveHint": true, "idempotentHint": false, "openWorldHint": true },
  },
  async ({ summary, key_facts }) => {
    await flushBuffer();
    trackSave();
    const results = [];

    try {
      const sid =
        sessionId ||
        `${AGENT_ID}-${localTimestamp()}`;
      const data = await mnemoRequest("POST", "/writeback", {
        session_id: sid,
        summary: `[SESSION END] ${summary}`,
        key_facts: key_facts || [],
        projects_referenced: [],
        decisions_made: [],
        agent_id: AGENT_ID,
        // Mnemo v3 — the user/agent wrote this recap, so source="user"
        // (most session_end calls come from an agent at the user's
        // explicit prompt). Default category is current_state since
        // session recaps are by definition "what's in flight" — the
        // regex auto-suggester would otherwise misfire on debug
        // narrative keywords like "bug" or "broke".
        source: "user",
        category: "current_state",
        additional_tags: ["session_end"],
      });
      results.push(`Mnemo save: OK (memory_id=${data.memory_id || "ok"})`);
    } catch (err) {
      results.push(`Mnemo save: FAILED (${err.message})`);
    }

    try {
      const gitStatus = execSync("git status --porcelain", {
        cwd: BRAIN_DIR,
        encoding: "utf-8",
      }).trim();
      if (gitStatus) {
        execSync("git add -A", { cwd: BRAIN_DIR });
        execSync(
          `git commit -m "brain: ${AGENT_ID} session end — ${localDateOnly()}"`,
          { cwd: BRAIN_DIR }
        );
        execSync("git push", { cwd: BRAIN_DIR });
        results.push("Brain commit + push: OK");
      } else {
        results.push("Brain commit: no changes to commit");
      }
    } catch (err) {
      results.push(`Brain commit: FAILED (${err.message})`);
    }

    // v2.15.0: advisory line when this session (bridge lifetime) never
    // touched the agent's own lane file — the Lane Protocol step agents
    // kept skipping. Runs AFTER the commit so a lane updated moments ago
    // counts. Under-nags on long-lived bridge processes by design.
    try {
      let lane = null;
      for (const c of [`${AGENT_ID}.md`, `${AGENT_ID}-session.md`]) {
        if (existsSync(join(BRAIN_DIR, c))) { lane = c; break; }
      }
      const startEpoch = sessionStartTime
        ? new Date(sessionStartTime).getTime() / 1000
        : null;
      if (lane && startEpoch) {
        const ct = execFileSync("git", ["log", "-1", "--format=%ct", "--", lane], {
          cwd: BRAIN_DIR,
          encoding: "utf-8",
          stdio: ["ignore", "pipe", "ignore"],
        }).trim();
        if (ct && Number(ct) < startEpoch) {
          const ageDays = Math.round((Date.now() / 1000 - Number(ct)) / 86400);
          results.push(
            `⚠️ ${lane} was NOT updated this session (last commit ${ageDays} days ago). ` +
            `Per the Lane Protocol: write_brain_file("${lane}", ...) with a date bump + what changed, then call session_end again.`
          );
        }
      }
    } catch {
      // advisory only — never block a session end
    }

    const elapsed = sessionStartTime
      ? `Session duration: ${Math.round(
          (Date.now() - new Date(sessionStartTime).getTime()) / 60000
        )} minutes.`
      : "";

    return {
      content: [
        {
          type: "text",
          text: `Session end complete.\n${results.join("\n")}\n${elapsed}\nTotal tool calls this session: ${toolCallCount}`,
        },
      ],
    };
  }
);

} // end if (BRAIN_AVAILABLE)

// ── Wiki tools (legacy WikAI static pages — conditional) ───────
// Only register if WIKI_DIR exists. The wiki is no longer recompiled;
// live document discovery moved to the Librarian (file_find, FrankenClaw).

if (WIKI_AVAILABLE) {

server.registerTool(
  "wiki_search",
  {
    description: "Search the static wiki pages (legacy WikAI archive) — project docs, entities, and concepts compiled from Mnemo before the wiki was retired. Uses grep under the hood. Returns matching filenames and context lines. NOTE: pages are a snapshot, no longer recompiled nightly. For live document discovery, prefer the Librarian's file_find tool (FrankenClaw) if it's available to you.",
    inputSchema: {
    query: z.string().describe("Search term or phrase to find in the wiki"),
    section: z
      .enum(["all", "projects", "entities", "concepts", "sources"])
      .optional()
      .describe("Limit search to a wiki section. Default: all"),
    max_results: z
      .number()
      .optional()
      .describe("Max files to return (default 10)"),
  },
    annotations: { "title": 'Search Wiki', "readOnlyHint": true, "idempotentHint": true },
  },
  async ({ query, section, max_results }) => {
    const limit = max_results || 10;
    const searchDir =
      section && section !== "all" ? join(WIKI_DIR, section) : WIKI_DIR;
    captureCall("wiki_search", `wiki search: "${query}" in ${section || "all"}`);
    try {
      const grepResult = execSync(
        `grep -ril --include='*.md' ${JSON.stringify(query)} ${JSON.stringify(searchDir)} 2>/dev/null | head -${limit}`,
        { encoding: "utf-8", timeout: 10000 }
      ).trim();

      if (!grepResult) {
        return {
          content: [
            {
              type: "text",
              text: `No wiki pages found for "${query}".` + (nudgeCheck() || ""),
            },
          ],
        };
      }

      const files = grepResult.split("\n");
      const results = [];

      for (const filePath of files) {
        const relPath = filePath.replace(WIKI_DIR + "/", "");
        try {
          const context = execSync(
            `grep -in -C 1 ${JSON.stringify(query)} ${JSON.stringify(filePath)} 2>/dev/null | head -12`,
            { encoding: "utf-8", timeout: 5000 }
          ).trim();
          results.push(`### ${relPath}\n\`\`\`\n${context}\n\`\`\``);
        } catch {
          results.push(`### ${relPath}\n(matched but could not extract context)`);
        }
      }

      const text = `Found ${files.length} wiki pages for "${query}":\n\n${results.join("\n\n")}`;
      return {
        content: [{ type: "text", text: text + (nudgeCheck() || "") }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Wiki search error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

server.registerTool(
  "wiki_read",
  {
    description: "Read a specific static wiki page by path (relative to ~/wiki/). Example: 'projects/peter-widget.md', 'entities/rocky.md'. Use wiki_search first to find the right page, then wiki_read to get the full content. Pages are a legacy WikAI snapshot — no longer recompiled nightly.",
    inputSchema: {
    path: z
      .string()
      .describe(
        "Relative path within ~/wiki/, e.g. 'projects/peter-widget.md' or 'entities/guy.md'"
      ),
  },
    annotations: { "title": 'Read Wiki Page', "readOnlyHint": true, "idempotentHint": true },
  },
  async ({ path: wikiPath }) => {
    captureCall("wiki_read", `read wiki: ${wikiPath}`);
    try {
      const clean = wikiPath.replace(/\.\./g, "").replace(/^\//, "");
      const fullPath = join(WIKI_DIR, clean);

      if (!fullPath.startsWith(WIKI_DIR)) {
        return {
          content: [{ type: "text", text: "Path traversal blocked." }],
          isError: true,
        };
      }

      const content = await readFile(fullPath, "utf-8");
      const MAX_CHARS = 12000;
      const truncated =
        content.length > MAX_CHARS
          ? content.slice(0, MAX_CHARS) +
            `\n\n---\n*[Truncated — ${content.length} chars total, showing first ${MAX_CHARS}]*`
          : content;

      return {
        content: [{ type: "text", text: truncated + (nudgeCheck() || "") }],
      };
    } catch (err) {
      return {
        content: [
          {
            type: "text",
            text: `Error reading wiki page "${wikiPath}": ${err.message}`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.registerTool(
  "wiki_index",
  {
    description: "Get the static wiki index — lists all projects, entities, and concepts in the legacy WikAI page set. Good starting point to see what the archive holds.",
    annotations: { "title": 'Read Wiki Index', "readOnlyHint": true, "idempotentHint": true },
  },
  async () => {
    try {
      const index = await readFile(join(WIKI_DIR, "index.md"), "utf-8");
      const MAX_CHARS = 8000;
      const truncated =
        index.length > MAX_CHARS
          ? index.slice(0, MAX_CHARS) +
            "\n\n---\n*[Index truncated — use wiki_search for specific topics]*"
          : index;
      return { content: [{ type: "text", text: truncated }] };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Error reading wiki index: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

} // end if (WIKI_AVAILABLE)

// ── Tool: passport_get_user_context ────────────────────────────

server.registerTool(
  "passport_get_user_context",
  {
    description: "Read the user's portable working-style passport. Returns a prompt-ready text block plus structured claims. Call at session start to calibrate tone, workflow defaults, and negative constraints.",
    inputSchema: {
    scopes: z
      .array(z.string())
      .optional()
      .describe(
        "Filter by scope tags (general, build_mode, debug_mode, research_mode, public_facing). Omit for all."
      ),
    platform: z
      .string()
      .optional()
      .describe(
        "Platform hint (chatgpt, claude, gemini). Reserved for Phase 2 adapter layer."
      ),
    max_claims: z
      .number()
      .int()
      .min(1)
      .max(100)
      .optional()
      .describe("Cap the number of claims returned (default: 20)"),
  },
    annotations: { "title": 'Get User Passport Context', "readOnlyHint": true, "idempotentHint": true },
  },
  async ({ scopes, platform, max_claims }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/context", {
        scopes: scopes || null,
        platform: platform || null,
        max_claims: max_claims || 20,
      });
      const n = data.claims?.length || 0;
      const o = data.overlays?.length || 0;
      return {
        content: [
          {
            type: "text",
            text: `${data.prompt_block}\n\n---\n*Structured: ${n} claim(s), ${o} overlay(s), passport v${data.passport_version}*`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Passport context error: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_observe_behavior ────────────────────────────

server.registerTool(
  "passport_observe_behavior",
  {
    description: "Record a candidate observation about the user's working style. REQUIRES 2+ evidence turn refs (minimum). Lands in pending queue; does NOT promote automatically. Never include credentials, project secrets, or client data.",
    inputSchema: {
    proposed_claim: z
      .string()
      .max(180)
      .describe(
        "Atomic, testable claim (≤180 chars). E.g. 'Prefers direct answers with minimal fluff.'"
      ),
    type: z
      .enum([
        "preference",
        "workflow_default",
        "negative_constraint",
        "style_default",
        "decision_pattern",
        "mode_trait",
      ])
      .describe("Claim type."),
    scope: z
      .array(z.string())
      .optional()
      .describe(
        "Scope tags: general, build_mode, debug_mode, research_mode, public_facing, personal, professional."
      ),
    confidence: z
      .number()
      .min(0)
      .max(1)
      .describe("Self-assessment 0.0–1.0."),
    proposed_target_section: z
      .string()
      .describe(
        "Dotted path for promotion. E.g. 'stable_core.communication', 'stable_core.workflow', 'negative_constraints'."
      ),
    source_platform: z
      .string()
      .describe(
        "Where the interaction happened (chatgpt, claude, cc, opie, rocky)."
      ),
    source_session_id: z
      .string()
      .describe("Session identifier (free-form)."),
    evidence: z
      .array(
        z.object({
          turn_ref: z.string().describe("Turn identifier, e.g. 'u12-a12'."),
          excerpt: z
            .string()
            .max(400)
            .describe(
              "Short verbatim excerpt (≤400 chars). Never a full transcript."
            ),
        })
      )
      .min(2)
      .describe("MINIMUM 2 evidence items. Fewer = rejected."),
  },
    annotations: { "title": 'Observe User Behavior (Pending Queue)', "readOnlyHint": false, "destructiveHint": false, "idempotentHint": false },
  },
  async (args) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/observe", args);
      if (data.status === "rejected") {
        const dup = data.duplicate_of
          ? ` (duplicate of ${data.duplicate_of})`
          : "";
        return {
          content: [
            {
              type: "text",
              text: `Observation rejected: ${data.rejection_reason}${dup}`,
            },
          ],
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Pending: ${data.observation_id}\nCommit: ${data.commit_sha?.slice(0, 7) || "—"}\nAwaiting passport_promote_observation to land in the stable passport.`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Passport observe error: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_list_pending_observations ───────────────────

server.registerTool(
  "passport_list_pending_observations",
  {
    description: "List candidate observations waiting in the pending queue. Filter by status (pending|promoted).",
    inputSchema: {
    status: z
      .enum(["pending", "promoted"])
      .optional()
      .describe("Filter by status (default: pending)"),
    limit: z
      .number()
      .int()
      .min(1)
      .max(200)
      .optional()
      .describe("Cap items returned (default: 25)"),
  },
    annotations: { "title": 'List Pending Passport Observations', "readOnlyHint": true, "idempotentHint": true },
  },
  async ({ status, limit }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/pending", {
        status: status || "pending",
        limit: limit || 25,
      });
      const items = data.items || [];
      if (items.length === 0) {
        return {
          content: [{ type: "text", text: "No pending observations." }],
        };
      }
      const lines = items.map(
        (o) =>
          `- ${o.observation_id} [${o.type}] conf=${o.confidence} → ${o.proposed_target_section}\n  "${o.proposed_claim}"`
      );
      return {
        content: [
          {
            type: "text",
            text: `${items.length} pending:\n\n${lines.join("\n\n")}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Passport list error: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_promote_observation ─────────────────────────

server.registerTool(
  "passport_promote_observation",
  {
    description: "Move a pending observation into the stable passport. Only promote claims you're confident in — this is the gate between candidate and canonical.",
    inputSchema: {
    observation_id: z
      .string()
      .describe("The obs_NNN id from passport_list_pending_observations."),
    target_section: z
      .string()
      .optional()
      .describe(
        "Override the observation's proposed target. Dotted path, e.g. 'stable_core.communication'."
      ),
    actor: z
      .string()
      .optional()
      .describe("Who is promoting (user, opie, cc, system). Default: system."),
  },
    annotations: { "title": 'Promote Observation to Stable Claim', "readOnlyHint": false, "destructiveHint": false, "idempotentHint": true },
  },
  async ({ observation_id, target_section, actor }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/promote", {
        observation_id,
        target_section: target_section || null,
        actor: actor || "system",
      });
      if (!data.promoted) {
        return {
          content: [{ type: "text", text: `Promotion failed: ${data.reason}` }],
          isError: true,
        };
      }
      return {
        content: [
          {
            type: "text",
            text: `Promoted ${observation_id} → ${data.claim_id} (${data.target_section})\nCommit: ${data.commit_sha?.slice(0, 7) || "—"}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Passport promote error: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Tool: passport_forget_or_override ──────────────────────────

server.registerTool(
  "passport_forget_or_override",
  {
    description: "Deprecate, forget, or replace an existing stable claim. Use override (with replacement_claim) to correct wording while preserving lineage. Use forget to remove a claim entirely. Use deprecate to retire without replacement.",
    inputSchema: {
    action: z
      .enum(["deprecate", "forget", "override", "replace"])
      .describe(
        "deprecate=retire; forget=remove; override/replace=new wording with supersedes link."
      ),
    target_claim_id: z
      .string()
      .describe("The claim_id to act on, e.g. 'pref_prefers_001'."),
    replacement_claim: z
      .string()
      .max(180)
      .optional()
      .describe(
        "Required for action=override/replace. The corrected claim text."
      ),
    reason: z
      .string()
      .optional()
      .describe("Free-text reason (lands in the audit log)."),
    actor: z
      .string()
      .optional()
      .describe("user | opie | cc | system. Default: user."),
  },
    annotations: { "title": 'Forget or Override Stable Claim', "readOnlyHint": false, "destructiveHint": true, "idempotentHint": true },
  },
  async ({ action, target_claim_id, replacement_claim, reason, actor }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/passport/override", {
        action,
        target_claim_id,
        replacement_claim: replacement_claim || null,
        reason: reason || null,
        actor: actor || "user",
      });
      if (!data.success) {
        return {
          content: [{ type: "text", text: `Action failed: ${data.reason}` }],
          isError: true,
        };
      }
      const line = data.new_claim_id
        ? `${action} ${target_claim_id} → ${data.new_claim_id}`
        : `${action} ${target_claim_id}`;
      return {
        content: [
          {
            type: "text",
            text: `${line}\nAudit: ${data.override_id}\nCommit: ${data.commit_sha?.slice(0, 7) || "—"}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          { type: "text", text: `Passport override error: ${err.message}` },
        ],
        isError: true,
      };
    }
  }
);

// ── Phase 3: Facts tools ───────────────────────────────────────
// Structured key-value facts with three-state confidence + audit history.
// Facts are workspace-shared (global), not per-agent. Use for "current truth"
// (Guy's location, Hoffman's GMC status) that should update in place rather
// than accumulate as parallel memory chunks.

server.registerTool(
  "mnemo_fact_get",
  {
    description: "Look up a single fact by (entity, attribute). Instant key-value retrieval, NOT semantic search. Use for queries like 'what's Guy's location' where you want one definitive answer. Returns {found: false} if missing. By default excludes facts with confidence='false' (use include_false=true to see them for audit).",
    inputSchema: {
      entity: z.string().describe("Thing the fact is about (person, machine, product, store, project). Case + spacing normalized."),
      attribute: z.string().describe("Property of the entity (location, owner, port, url, version). Snake_case normalized."),
      include_false: z.boolean().optional().describe("Include facts that were disproven. Default false."),
    },
    annotations: { "title": "Get Fact", "readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false },
  },
  async ({ entity, attribute, include_false }) => {
    captureCall("mnemo_fact_get", `${entity}/${attribute}`);
    try {
      const qs = include_false ? "?include_false=true" : "";
      const data = await mnemoRequest("GET", `/facts/${encodeURIComponent(entity)}/${encodeURIComponent(attribute)}${qs}`);
      if (!data.found) return { content: [{ type: "text", text: `No fact for (${entity}, ${attribute})` }] };
      const lines = [
        `${data.entity} . ${data.attribute} = ${data.value}`,
        `  confidence: ${data.confidence}`,
        `  evidence:   ${data.evidence_source}`,
        `  source:     ${data.source_agent || "—"}${data.source_memory_id ? " (memory:" + data.source_memory_id + ")" : ""}`,
        `  updated:    ${new Date(data.last_updated * 1000).toISOString()}`,
      ];
      return { content: [{ type: "text", text: lines.join("\n") }] };
    } catch (err) {
      return { content: [{ type: "text", text: `Fact lookup error: ${err.message}` }], isError: true };
    }
  }
);

server.registerTool(
  "mnemo_fact_query",
  {
    description: "List facts matching filters. All filters optional; AND-joined. Use for queries like 'all facts about Guy' or 'all unverified facts'. Default limit 20, max 100.",
    inputSchema: {
      entity: z.string().optional().describe("Filter to facts about this entity."),
      attribute: z.string().optional().describe("Filter to this attribute name."),
      value_contains: z.string().optional().describe("Substring match on the value."),
      confidence: z.enum(["verified", "high_probability", "false"]).optional().describe("Filter by confidence level."),
      limit: z.number().int().min(1).max(100).optional().describe("Max results. Default 20."),
    },
    annotations: { "title": "Query Facts", "readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false },
  },
  async ({ entity, attribute, value_contains, confidence, limit }) => {
    captureCall("mnemo_fact_query", `${entity || "*"}/${attribute || "*"}`);
    try {
      const params = new URLSearchParams();
      if (entity !== undefined) params.set("entity", entity);
      if (attribute !== undefined) params.set("attribute", attribute);
      if (value_contains !== undefined) params.set("value_contains", value_contains);
      if (confidence !== undefined) params.set("confidence", confidence);
      if (limit !== undefined) params.set("limit", String(limit));
      const qs = params.toString();
      const data = await mnemoRequest("GET", `/facts${qs ? "?" + qs : ""}`);
      if (data.count === 0) return { content: [{ type: "text", text: "No facts match." }] };
      const lines = [`Found ${data.count} fact(s):`];
      for (const f of data.facts) {
        lines.push(`  ${f.entity} . ${f.attribute} = ${f.value}  [${f.confidence}]  ${f.evidence_source}`);
      }
      return { content: [{ type: "text", text: lines.join("\n") }] };
    } catch (err) {
      return { content: [{ type: "text", text: `Fact query error: ${err.message}` }], isError: true };
    }
  }
);

server.registerTool(
  "mnemo_fact_save",
  {
    description: "Assert a structured fact. UPSERT semantics — same (entity, attribute) updates in place. Different value triggers contradiction handling: higher-or-equal confidence overwrites with audit log; lower confidence is REJECTED if existing is higher. Use confidence='verified' only when confirmed (source code, direct Guy statement, tool output). Use 'high_probability' for strong inference. evidence_source uses prefix convention: memory:<id>, commit:<sha>, file:<path>:<line>, statement:<who>, bus:#<id>, dream:<date>.",
    inputSchema: {
      entity: z.string().describe("Thing the fact is about. Normalized lowercase."),
      attribute: z.string().describe("Property of the entity. Normalized snake_case."),
      value: z.string().describe("Current truth value."),
      confidence: z.enum(["verified", "high_probability", "false"]).describe("verified = confirmed from source. high_probability = strong inference. false = known wrong."),
      evidence_source: z.string().describe("Where the confidence comes from. Use prefixes: memory:<id>, commit:<sha>, statement:<who>, etc."),
      source_memory_id: z.string().optional().describe("Pointer to the originating memory entry, if any."),
    },
    annotations: { "title": "Save Fact", "readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": true },
  },
  async ({ entity, attribute, value, confidence, evidence_source, source_memory_id }) => {
    captureCall("mnemo_fact_save", `${entity}/${attribute}=${value.slice(0, 60)}`);
    try {
      const data = await mnemoRequest("POST", "/facts", {
        entity, attribute, value, confidence, evidence_source,
        source_memory_id: source_memory_id || null,
        source_agent: AGENT_ID,
      });
      const lines = [
        data.written ? `Fact saved (${data.reason})` : `Fact REJECTED (${data.reason})`,
        `  ${entity} . ${attribute} = ${value}  [${confidence}]`,
      ];
      if (data.was_contradiction) {
        lines.push(`  contradiction with previous: ${data.previous_value} [${data.previous_confidence}]`);
      }
      return { content: [{ type: "text", text: lines.join("\n") }], isError: !data.written };
    } catch (err) {
      return { content: [{ type: "text", text: `Fact save error: ${err.message}` }], isError: true };
    }
  }
);

server.registerTool(
  "mnemo_fact_demote",
  {
    description: "Force a fact to confidence='false' without supplying a new value. Use when you know something is wrong but don't yet know the correct answer. Required because the normal save path's promotion ladder blocks verified→false transitions that lack a replacement value.",
    inputSchema: {
      entity: z.string().describe("Thing the fact is about."),
      attribute: z.string().describe("Property to demote."),
      reason: z.string().describe("Why this fact is wrong. Required, logged to history."),
    },
    annotations: { "title": "Demote Fact", "readOnlyHint": false, "destructiveHint": true, "idempotentHint": true, "openWorldHint": true },
  },
  async ({ entity, attribute, reason }) => {
    captureCall("mnemo_fact_demote", `${entity}/${attribute}`);
    try {
      const data = await mnemoRequest("POST", "/facts/demote", {
        entity, attribute, reason, changed_by: AGENT_ID,
      });
      if (!data.written) return { content: [{ type: "text", text: `Demote: ${data.reason}` }] };
      return {
        content: [{ type: "text", text: `Demoted ${entity}.${attribute} (was ${data.previous_confidence}: ${data.previous_value}) — reason: ${reason}` }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Fact demote error: ${err.message}` }], isError: true };
    }
  }
);

// ── Tools: capture pause gate (v4.1) ───────────────────────────
// "I'm about to handle a secret — stop recording." Pauses ambient capture
// server-wide (auto-sync, captureCall flushes, /ingest). Dead-man switch:
// auto-resumes at expiry even if everyone forgets. Manual mnemo_save still
// works while paused — saving the *why* of a sensitive op is the intended
// workflow.

server.registerTool(
  "mnemo_capture_pause",
  {
    description: "Pause Mnemo's ambient auto-capture before sensitive operations (key rotations, credential pastes). Auto-resumes after `minutes` (default 15, max 240) even if you forget — a dead-man switch, not a toggle. Deliberate mnemo_save calls still work while paused. Activity during the pause window is DISCARDED, not buffered.",
    inputSchema: {
      minutes: z
        .number()
        .int()
        .min(1)
        .max(240)
        .optional()
        .describe("Pause duration in minutes. Default 15."),
      reason: z
        .string()
        .max(200)
        .optional()
        .describe("Why capture is paused — shown in /capture/status."),
    },
    annotations: { "title": "Pause Auto-Capture", "readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": true },
  },
  async ({ minutes, reason }) => {
    try {
      const body = { reason: reason || "" };
      if (minutes !== undefined) body.minutes = minutes;
      const data = await mnemoRequest("POST", "/capture/pause", body);
      const mins = Math.round((data.remaining_seconds || 0) / 60);
      return {
        content: [{
          type: "text",
          text: `⏸ Ambient capture PAUSED for ~${mins} min — ${data.reason}\nAuto-resumes at expiry. Resume early with mnemo_capture_resume. Manual mnemo_save still works.`,
        }],
      };
    } catch (err) {
      return { content: [{ type: "text", text: `Capture pause error: ${err.message}` }], isError: true };
    }
  }
);

server.registerTool(
  "mnemo_capture_resume",
  {
    description: "Resume Mnemo's ambient auto-capture after a mnemo_capture_pause (early — it would auto-resume on its own at expiry).",
    inputSchema: {},
    annotations: { "title": "Resume Auto-Capture", "readOnlyHint": false, "destructiveHint": false, "idempotentHint": true, "openWorldHint": true },
  },
  async () => {
    try {
      await mnemoRequest("POST", "/capture/resume", {});
      return { content: [{ type: "text", text: "▶ Ambient capture RESUMED." }] };
    } catch (err) {
      return { content: [{ type: "text", text: `Capture resume error: ${err.message}` }], isError: true };
    }
  }
);

// ── Start ──────────────────────────────────────────────────────

const harnessToolNotice = harnessToolGate.startupNotice();
if (harnessToolNotice) process.stderr.write(harnessToolNotice);
await checkHealth();
const transport = new StdioServerTransport();
await server.connect(transport);
