import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// ── Configuration ──────────────────────────────────────────────
// MNEMO_URL: where your Mnemo Cortex API lives
// MNEMO_AGENT_ID: who this agent is in the memory system
//
// OpenClaw users set these via env vars in their MCP config.
// Defaults point to a local Mnemo Cortex instance.

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";
const AGENT_ID = process.env.MNEMO_AGENT_ID || "openclaw";

// ── Mnemo API client ───────────────────────────────────────────
// Two methods: POST for writes/searches, GET for reads.
// Errors surface as tool errors — the agent sees what went wrong.

async function mnemoRequest(method, path, body) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body) opts.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(`${MNEMO_URL}${path}`, opts);
  } catch (err) {
    throw new Error(
      `Cannot reach Mnemo Cortex at ${MNEMO_URL}. Is it running? (${err.message})`
    );
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Mnemo ${method} ${path} → ${res.status}: ${text}`);
  }

  return res.json();
}

// ── Health check on startup ────────────────────────────────────
// Verify Mnemo Cortex is reachable before accepting connections.
// If it's down, the agent gets a clear error on first tool use
// instead of a confusing connection failure mid-conversation.

let mnemoHealthy = false;

async function checkHealth() {
  try {
    const h = await mnemoRequest("GET", "/health");
    if (h.status === "ok") {
      mnemoHealthy = true;
      process.stderr.write(
        `[mnemo-mcp] Connected to Mnemo Cortex at ${MNEMO_URL} (${h.memory_entries} memories)\n`
      );
    }
  } catch (err) {
    process.stderr.write(
      `[mnemo-mcp] WARNING: Mnemo Cortex not reachable at ${MNEMO_URL}. Tools will retry on each call.\n`
    );
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
  version: "1.0.0",
});

// ── Tool: mnemo_recall ─────────────────────────────────────────
// Semantic recall within this agent's own memories.
// Use this when the agent needs to remember something from
// its own past sessions.

server.tool(
  "mnemo_recall",
  `Recall memories from Mnemo Cortex for the current agent (${AGENT_ID}). Returns semantically relevant chunks from past sessions.`,
  {
    query: z.string().describe("What to search for in memory"),
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
// Cross-agent search. Any agent can read any other agent's
// memories. This is how agents share knowledge.

server.tool(
  "mnemo_search",
  "Search ALL agent memories in Mnemo Cortex (cross-agent). Use this to find memories from Rocky, CC, Opie, or any other agent.",
  {
    query: z.string().describe("What to search for across all agents"),
    agent_id: z
      .string()
      .optional()
      .describe(
        "Filter to a specific agent (rocky, cc, opie). Omit for all."
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
      const body = {
        prompt: query,
        max_results: max_results || 5,
      };
      if (agent_id) body.agent_id = agent_id;

      const data = await mnemoRequest("POST", "/context", body);
      const chunks = data.chunks || [];
      const text = formatChunks(chunks, true);
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
        content: [{ type: "text", text: `Search error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// ── Tool: mnemo_save ───────────────────────────────────────────
// Write a memory to Mnemo Cortex. Use at session end or when
// something important happens that should be remembered.

server.tool(
  "mnemo_save",
  "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered.",
  {
    summary: z
      .string()
      .describe("Summary of what happened or what to remember"),
    key_facts: z
      .array(z.string())
      .optional()
      .describe("List of key facts to store"),
    session_id: z
      .string()
      .optional()
      .describe("Session identifier. Auto-generated if omitted."),
  },
  async ({ summary, key_facts, session_id }) => {
    try {
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

// ── Start ──────────────────────────────────────────────────────

await checkHealth();
const transport = new StdioServerTransport();
await server.connect(transport);
