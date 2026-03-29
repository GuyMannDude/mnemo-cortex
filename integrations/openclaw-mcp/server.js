import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// ── Configuration ──────────────────────────────────────────────
// MNEMO_URL: where your Mnemo Cortex API lives
// MNEMO_AGENT_ID: who this agent is in the memory system
// MNEMO_SHARE: cross-agent sharing mode (separate|always|never)
//
// OpenClaw users set these via env vars in their MCP config.
// Defaults point to a local Mnemo Cortex instance.

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";
const AGENT_ID = process.env.MNEMO_AGENT_ID || "openclaw";

const SHARE_MODES = ["separate", "always", "never"];
const shareMode = SHARE_MODES.includes(process.env.MNEMO_SHARE)
  ? process.env.MNEMO_SHARE
  : "separate";
let sessionShareActive = shareMode === "always";

const FETCH_TIMEOUT_MS = 10_000;

// ── Mnemo API client ───────────────────────────────────────────
// Two methods: POST for writes/searches, GET for reads.
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
    throw new Error(
      "Cannot reach Mnemo Cortex. Is it running?"
    );
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
// Verify Mnemo Cortex is reachable. If healthy, tools work
// immediately. If not, tools will attempt to connect on each
// call and return clear errors if Mnemo is still down.

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
    // Mnemo was down at startup — try once more before failing
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

function formatChunks(chunks, showAgent) {
  if (!chunks || chunks.length === 0) return "No memories found.";

  return chunks
    .map((c) => {
      const rel = (c.relevance || 0).toFixed(2);
      const tier = c.cache_tier || "?";
      const header = showAgent
        ? `[${tier}] agent=${c.agent_id || "?"} (relevance: ${rel})`
        : `[${tier}] (relevance: ${rel})`;
      return `### ${header}\n${c.content}`;
    })
    .join("\n\n");
}

// ── MCP Server ─────────────────────────────────────────────────

const server = new McpServer({
  name: "mnemo-cortex",
  version: "2.0.0",
});

// ── Tool: mnemo_recall ─────────────────────────────────────────
// Semantic recall within this agent's own memories.
// Use this when the agent needs to remember something from
// its own past sessions. Not affected by share mode.

server.tool(
  "mnemo_recall",
  `Recall memories from Mnemo Cortex for the current agent (${AGENT_ID}). Returns semantically relevant chunks from past sessions.`,
  {
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
      .describe("Maximum number of memories to return (default: 5)"),
  },
  async ({ query, max_results }) => {
    try {
      await ensureHealth();
      const data = await mnemoRequest("POST", "/context", {
        prompt: query,
        agent_id: AGENT_ID,
        max_results: max_results || 5,
      });

      const chunks = data.chunks || [];
      const text = formatChunks(chunks, false);
      const count = data.total_found || chunks.length;

      return {
        content: [
          {
            type: "text",
            text: count > 0 ? `Found ${count} memories:\n\n${text}` : text,
          },
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
// Cross-agent search. Gated by share mode:
// - separate (default): restricted to own agent_id
// - always: cross-agent search enabled
// - never: permanently restricted, toggle blocked
// - session toggle via mnemo_share tool

server.tool(
  "mnemo_search",
  "Search memories in Mnemo Cortex. By default, searches only your own memories. Use mnemo_share to enable cross-agent search for this session.",
  {
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
      .describe("Maximum number of memories to return (default: 5)"),
  },
  async ({ query, agent_id, max_results }) => {
    try {
      await ensureHealth();
      const body = {
        prompt: query,
        max_results: max_results || 5,
      };

      if (sessionShareActive) {
        // Cross-agent search allowed
        if (agent_id) body.agent_id = agent_id;
        // If no agent_id, search all agents (no filter)
      } else {
        // Restricted — force filter to own agent
        body.agent_id = AGENT_ID;
      }

      const data = await mnemoRequest("POST", "/context", body);
      const chunks = data.chunks || [];
      const text = formatChunks(chunks, sessionShareActive);
      const count = data.total_found || chunks.length;

      let prefix = "";
      if (!sessionShareActive) {
        prefix =
          "(Restricted to your own memories. Use mnemo_share to enable cross-agent search.)\n\n";
      }

      return {
        content: [
          {
            type: "text",
            text:
              count > 0
                ? `${prefix}Found ${count} memories:\n\n${text}`
                : `${prefix}${text}`,
          },
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
// Write a memory to Mnemo Cortex. Use at session end or when
// something important happens that should be remembered.
// Always writes to this agent's own memory slot.

server.tool(
  "mnemo_save",
  "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered.",
  {
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
  async ({ summary, key_facts, session_id }) => {
    try {
      await ensureHealth();
      const sid =
        session_id ||
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
// Respects the MNEMO_SHARE config: never blocks, always is a
// no-op, separate allows per-session toggle.

server.tool(
  "mnemo_share",
  "Toggle cross-agent memory sharing for this session. When on, mnemo_search can read memories from all agents. When off, search is limited to this agent only.",
  {},
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

// ── Start ──────────────────────────────────────────────────────

await checkHealth();
const transport = new StdioServerTransport();
await server.connect(transport);
