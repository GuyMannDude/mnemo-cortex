import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const MNEMO_URL = process.env.MNEMO_URL || "http://localhost:50001";
const AGENT_ID = process.env.MNEMO_AGENT_ID || "claude-desktop";

async function mnemoPost(path, body) {
  const res = await fetch(`${MNEMO_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Mnemo ${path} ${res.status}: ${text}`);
  }
  return res.json();
}

async function mnemoGet(path) {
  const res = await fetch(`${MNEMO_URL}${path}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Mnemo ${path} ${res.status}: ${text}`);
  }
  return res.json();
}

const server = new McpServer({
  name: "mnemo-cortex",
  version: "1.0.0",
});

// --- mnemo_recall: semantic recall for this agent ---
server.tool(
  "mnemo_recall",
  "Recall memories from Mnemo Cortex for the current agent. Returns semantically relevant chunks from past sessions.",
  { query: z.string().describe("What to search for in memory") },
  async ({ query }) => {
    try {
      const data = await mnemoPost("/context", {
        prompt: query,
        agent_id: AGENT_ID,
        max_results: 5,
      });
      const chunks = data.chunks || [];
      if (chunks.length === 0) {
        return { content: [{ type: "text", text: "No memories found." }] };
      }
      const text = chunks
        .map((c, i) => {
          const tier = c.cache_tier || "?";
          const rel = (c.relevance || 0).toFixed(2);
          return `### [${tier}] (relevance: ${rel})\n${c.content}`;
        })
        .join("\n\n");
      return {
        content: [
          {
            type: "text",
            text: `Found ${data.total_found || chunks.length} memories:\n\n${text}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- mnemo_search: cross-agent search (all memories) ---
server.tool(
  "mnemo_search",
  "Search ALL agent memories in Mnemo Cortex (cross-agent). Use this to find memories from any agent in the system.",
  {
    query: z.string().describe("What to search for across all agents"),
    agent_id: z
      .string()
      .optional()
      .describe("Filter to a specific agent ID. Omit to search all agents."),
  },
  async ({ query, agent_id }) => {
    try {
      const body = {
        prompt: query,
        max_results: 5,
      };
      if (agent_id) body.agent_id = agent_id;
      const data = await mnemoPost("/context", body);
      const chunks = data.chunks || [];
      if (chunks.length === 0) {
        return { content: [{ type: "text", text: "No memories found." }] };
      }
      const text = chunks
        .map((c) => {
          const tier = c.cache_tier || "?";
          const rel = (c.relevance || 0).toFixed(2);
          const agent = c.agent_id || "?";
          return `### [${tier}] agent=${agent} (relevance: ${rel})\n${c.content}`;
        })
        .join("\n\n");
      return {
        content: [
          {
            type: "text",
            text: `Found ${data.total_found || chunks.length} memories:\n\n${text}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// --- mnemo_save: write a memory/summary back to Mnemo Cortex ---
server.tool(
  "mnemo_save",
  "Save a summary or key facts to Mnemo Cortex for future recall. Use at session end or when something important should be remembered.",
  {
    summary: z.string().describe("Summary of what happened or what to remember"),
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
      const data = await mnemoPost("/writeback", {
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
            text: `Saved to Mnemo Cortex: memory_id=${data.memory_id || "ok"}, session=${sid}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: `Error: ${err.message}` }],
        isError: true,
      };
    }
  }
);

// Start
const transport = new StdioServerTransport();
await server.connect(transport);
