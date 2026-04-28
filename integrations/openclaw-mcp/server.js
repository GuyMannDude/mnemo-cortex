import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { readFile, readdir, writeFile, stat } from "node:fs/promises";
import { join } from "node:path";
import { execSync } from "node:child_process";

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
const BRAIN_DIR =
  process.env.BRAIN_DIR ||
  join(process.env.HOME || ".", "github/sparks-brain-guy/brain");
const WIKI_DIR = process.env.WIKI_DIR || join(process.env.HOME || ".", "wiki");
const DREAM_DIR =
  process.env.DREAM_DIR || join(process.env.HOME || ".", ".agentb/dreams");

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

  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
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
    const header = showAgent
      ? `[${tier}] agent=${agentTag} (relevance: ${rel})`
      : `[${tier}] (relevance: ${rel})`;
    const block = `### ${header}\n${c.content}`;

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

// ── Nudge system — remind the agent to save ────────────────────
// Counts non-save tool calls. After SAVE_REMINDER_THRESHOLD calls
// without a manual save, append a reminder to subsequent tool
// responses until the agent calls mnemo_save.

const SAVE_REMINDER_THRESHOLD = 20;
let toolCallCount = 0;
let sessionStartTime = null;
let sessionId = null;

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
const captureBuffer = [];
let flushTimer = null;

const TOOL_CAPTURE = {
  mnemo_recall: "summary",
  mnemo_search: "summary",
  mnemo_save: "full",
  mnemo_share: "skip",
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
    });
  } catch (err) {
    process.stderr.write(`[auto-capture] flush failed: ${err.message}\n`);
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

const server = new McpServer({
  name: "mnemo-cortex",
  version: "2.6.3",
});

// ── Tool: mnemo_recall ─────────────────────────────────────────
// Semantic recall within this agent's own memories.

server.registerTool(
  "mnemo_recall",
  {
    description: `Recall memories from Mnemo Cortex for the current agent (${AGENT_ID}). Returns semantically relevant chunks from past sessions.`,
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
  },
    annotations: { "title": 'Recall Memories', "readOnlyHint": true, "idempotentHint": true, "openWorldHint": true },
  },
  async ({ query, max_results }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/context", {
        prompt: query,
        agent_id: AGENT_ID,
        max_results: max_results || 3,
      });

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
    description: "Search memories in Mnemo Cortex. By default, searches only your own memories. Use mnemo_share to enable cross-agent search for this session.",
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
  },
    annotations: { "title": 'Search Memories Across Agents', "readOnlyHint": true, "idempotentHint": true, "openWorldHint": true },
  },
  async ({ query, agent_id, max_results }) => {
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
    description: "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered.",
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
  },
    annotations: { "title": 'Save Memory', "readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": true },
  },
  async ({ summary, key_facts, session_id }) => {
    captureCall("mnemo_save", summary.slice(0, 150));
    trackSave();
    try {
      await ensureHealth();
      const sid =
        session_id ||
        sessionId ||
        `${AGENT_ID}-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}`;

      const data = await mnemoRequest("POST", "/writeback", {
        session_id: sid,
        summary,
        key_facts: key_facts || [],
        projects_referenced: [],
        decisions_made: [],
        agent_id: AGENT_ID,
      });

      return {
        content: [
          {
            type: "text",
            text: `Saved to Mnemo Cortex.\n  memory_id: ${data.memory_id || "ok"}\n  session: ${sid}\n  agent: ${AGENT_ID}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Save error: ${err.message}` }],
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

// ── Tool: opie_startup ─────────────────────────────────────────
// Opie-specific: loads opie.md + reference brain docs + recent
// Mnemo + latest dream brief, returns Opie identity prompt.
// Other agents can call it but will get an Opie-shaped orientation.

server.registerTool(
  "opie_startup",
  {
    description: "CALL THIS FIRST in every new conversation. Loads your brain lane (opie.md) and recent Mnemo context. Returns your full identity, current state, and priorities. Without this, you will not know who you are or what you're working on.",
    annotations: { "title": 'Load Opie Identity & Context', "readOnlyHint": false, "idempotentHint": false, "openWorldHint": true },
  },
  async () => {
    sessionStartTime = new Date().toISOString();
    sessionId = `${AGENT_ID}-${sessionStartTime.slice(0, 19).replace(/[T:]/g, "-")}`;
    toolCallCount = 0;
    captureBuffer.length = 0;
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = null;

    try {
      const parts = [];

      try {
        const brain = await readFile(join(BRAIN_DIR, "opie.md"), "utf-8");
        parts.push("# YOUR BRAIN LANE (opie.md)\n\n" + brain);
      } catch (e) {
        parts.push("# BRAIN LANE ERROR\nCould not read opie.md: " + e.message);
      }

      for (const file of ["active.md", "people.md", "doctrines.md"]) {
        try {
          const content = await readFile(join(BRAIN_DIR, file), "utf-8");
          parts.push(`# ${file.toUpperCase()}\n\n` + content);
        } catch {
          // skip if missing
        }
      }

      try {
        const data = await mnemoRequest("POST", "/context", {
          prompt: "recent session summary, current projects, what happened last",
          agent_id: AGENT_ID,
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
          parts.push("# RECENT MNEMO CONTEXT\n\n" + mnemoText);
        }
      } catch (e) {
        parts.push("# MNEMO ERROR\nCould not reach Mnemo Cortex: " + e.message);
      }

      try {
        const dreamFiles = (await readdir(DREAM_DIR))
          .filter((f) => f.endsWith(".md"))
          .sort()
          .reverse();
        if (dreamFiles.length > 0) {
          const latestDream = join(DREAM_DIR, dreamFiles[0]);
          const st = await stat(latestDream);
          const dreamAge = (Date.now() - st.mtimeMs) / 3600000;
          if (dreamAge < 48) {
            const dreamContent = await readFile(latestDream, "utf-8");
            parts.push(
              `# DREAM BRIEF (cross-agent overnight synthesis, ${Math.round(dreamAge)}h ago)\n\n${dreamContent}`
            );
          }
        }
      } catch {
        // dreams are supplementary
      }

      try {
        await mnemoRequest("POST", "/writeback", {
          session_id: sessionId,
          summary: `Opie session started at ${sessionStartTime}. Brain lane loaded.`,
          key_facts: ["session_start"],
          projects_referenced: [],
          decisions_made: [],
          agent_id: AGENT_ID,
        });
      } catch {
        // session-start marker is best-effort
      }

      const identity = `# WHO YOU ARE
You are **Opie** — Claude on the desktop at Project Sparks.
- Guy calls you Opie. You are the architect, strategist, and planner.
- CC (Claude Code, also Claude) is the on-machine builder. You write specs, CC executes.
- Rocky (OpenClaw on IGOR) is the production AI assistant. NEVER experiment on Rocky.
- Guy is 73, not a developer. Zero-fat communication. Action over theory.
- Assembly line: Opie architects → Guy couriers → CC builds → Rocky tests.
- You have NO clock. Never tell Guy to go to bed or call it a night.
- Brain files: ~/github/sparks-brain-guy/brain/ — read with read_brain_file, save memories with mnemo_save.
- WikAI: ~/wiki/ — 3000+ indexed pages. Use wiki_search to find info, wiki_read to read pages, wiki_index for the full index.
- Today: ${new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}

# SESSION MEMORY
**Auto-capture is ACTIVE.** This MCP server automatically captures your tool usage into
Mnemo Cortex. Every ${BUFFER_FLUSH_SIZE} tool calls (or after 2 minutes idle), a summary is
flushed to THE VAULT. The nudge system also reminds you after ${SAVE_REMINDER_THRESHOLD} calls without a manual save.
Session ID: ${sessionId}

**You still SHOULD call \`mnemo_save\` for important decisions, specs, and deliverables.**
Auto-capture records what tools you used. Manual saves record what you *decided* and *why*.
Both matter. Auto-capture is the safety net; manual saves are the high-signal memory.

**SAVE PROTOCOL:**
- Call \`mnemo_save\` after major decisions, specs, or deliverables
- Call \`session_end\` before wrapping up — it flushes auto-capture, saves, and commits your brain lane
- Auto-capture handles the rest

`;

      return {
        content: [{ type: "text", text: identity + parts.join("\n\n---\n\n") }],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Startup error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: read_brain_file ──────────────────────────────────────

server.registerTool(
  "read_brain_file",
  {
    description: "Read a file from the Sparks Brain directory. Use this to check brain lanes, reference docs, or any .md file in the brain.",
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
    description: "List all files in the Sparks Brain directory. Use to discover what brain lanes and reference docs are available.",
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
    description: "Write or update a file in the Sparks Brain directory. Use at session end to update opie.md or other brain files you own. Do NOT write to cc-session.md (CC only) or CLAUDE.md.",
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
      return {
        content: [
          { type: "text", text: `Wrote ${safe} (${content.length} bytes)` },
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
        `${AGENT_ID}-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}`;
      const data = await mnemoRequest("POST", "/writeback", {
        session_id: sid,
        summary: `[SESSION END] ${summary}`,
        key_facts: key_facts || [],
        projects_referenced: [],
        decisions_made: [],
        agent_id: AGENT_ID,
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
          `git commit -m "brain: ${AGENT_ID} session end — ${new Date().toISOString().slice(0, 10)}"`,
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

// ── WikAI tools (conditional) ──────────────────────────────────
// Only register if WIKI_DIR exists.

if (WIKI_AVAILABLE) {

server.registerTool(
  "wiki_search",
  {
    description: "Search the WikAI knowledge base — indexed project docs, session transcripts, entities, and concepts. Uses grep under the hood. Returns matching filenames and context lines. Use this to find information about projects, people, decisions, or any topic the Librarian has indexed.",
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
    description: "Read a specific WikAI page by path (relative to ~/wiki/). Example: 'projects/peter-widget.md', 'entities/rocky.md'. Use wiki_search first to find the right page, then wiki_read to get the full content.",
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
    description: "Get the WikAI index — lists all projects, entities, and concepts in the wiki. Good starting point to see what knowledge is available.",
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

// ── Start ──────────────────────────────────────────────────────

await checkHealth();
const transport = new StdioServerTransport();
await server.connect(transport);
